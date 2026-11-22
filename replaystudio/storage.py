from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path
from shutil import copyfileobj

from .model import (
    MAX_CUE_SECONDS, MAX_MEDIA_FRAME, Asset, Box, Cue, CueVoice, Initiative, MediaBookmark,
    MediaClip, MediaEdit, MediaTrack, Project, ReplayError, Role, RoleVoice, Scene,
    TEMPLATE_MINIMUMS, Template, Turn, VoiceSettings, parse_log,
)

PROJECT_VERSION = 2
LEGACY_PROJECT_VERSION = 1
PROJECT_SUFFIX = ".replay"
TEMPLATE_VERSION = 1
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
MUSIC_EXTENSIONS = {".mp3", ".wav"}
VOICE_EXTENSIONS = {".mp3", ".wav", ".ogg"}


def asset_path(asset: Asset, directory: Path) -> Path:
    return directory / asset.stored_name


def import_bytes(project: Project, data: bytes, name: str, extension: str,
                 directory: Path) -> Asset:
    extension = extension.lower()
    if extension not in VOICE_EXTENSIONS | IMAGE_EXTENSIONS:
        raise ReplayError("不受支持的素材格式。")
    asset_id = hashlib.sha256(data).hexdigest()
    if existing := project.assets.get(asset_id):
        if asset_path(existing, directory).is_file():
            return existing
    asset = Asset(asset_id, name, extension)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as staged:
            temporary = Path(staged.name)
            staged.write(data)
        temporary.replace(asset_path(asset, directory))
        project.assets[asset.id] = asset
        return asset
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def import_asset(project: Project, source: Path, directory: Path,
                 extensions: set[str]) -> Asset:
    extension = source.suffix.lower()
    if extension not in extensions:
        raise ReplayError("请选择受支持的素材格式。")
    digest = hashlib.sha256()
    temporary = None
    try:
        with source.open("rb") as stream, tempfile.NamedTemporaryFile(
            dir=directory, suffix=".tmp", delete=False,
        ) as staged:
            temporary = Path(staged.name)
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                staged.write(chunk)
        asset_id = digest.hexdigest()
        if existing := project.assets.get(asset_id):
            if asset_path(existing, directory).is_file():
                return existing
        asset = Asset(asset_id, source.name, extension)
        temporary.replace(asset_path(asset, directory))
        project.assets[asset.id] = asset
        return asset
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _encode_json(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def _bounded_int(value: object, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ReplayError("数值超出可用范围。")
    return value


def _template_from_data(data: dict) -> Template:
    if not isinstance(data["name"], str):
        raise ReplayError("模板名称无效。")
    colors = {key: data[key] for key in ("body_color", "speaker_color", "box_color")}
    if not all(isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c) for c in colors.values()):
        raise ReplayError("模板颜色无效。")
    boxes = {
        name: Box(**{key: _bounded_int(data[name][key], 0, 2000)
                     for key in ("x", "y", "width", "height")}).bounded(*minimum)
        for name, minimum in TEMPLATE_MINIMUMS.items()
    }
    return Template(
        name=data["name"],
        **boxes,
        **colors,
        body_size=_bounded_int(data["body_size"], 18, 80),
        speaker_size=_bounded_int(data["speaker_size"], 18, 76),
        box_opacity=_bounded_int(data["box_opacity"], 0, 100),
        background_opacity=_bounded_int(data["background_opacity"], 0, 100),
    )


def _temporary_file(destination: Path) -> Path:
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".replay-", delete=False) as temporary:
        return Path(temporary.name)


def save_template(template: Template, destination: Path) -> None:
    staged = _temporary_file(destination)
    try:
        staged.write_bytes(_encode_json({"version": TEMPLATE_VERSION, "template": asdict(template)}))
        staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)


def open_template(source: Path) -> Template:
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        if data["version"] != TEMPLATE_VERSION:
            raise ReplayError("这个模板的版本暂不支持。")
        return _template_from_data(data["template"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, UnicodeError) as error:
        raise ReplayError("模板文件无法读取。") from error


def save_project(project: Project, destination: Path, directory: Path) -> None:
    referenced = project.used_assets()
    used = [asset for asset in project.assets.values() if asset.id in referenced]
    metadata = {
        "version": PROJECT_VERSION if project.media is not None else LEGACY_PROJECT_VERSION,
        "title": project.title,
        "source_name": project.source_name,
        "roles": [asdict(role) for role in project.roles.values()],
        "scenes": [asdict(scene) for scene in project.scenes.values()],
        "default_scene": project.default_scene,
        "cues": [asdict(cue) for cue in project.cues],
        "template": asdict(project.template),
        "reading_speed": project.reading_speed,
        "assets": [asdict(asset) for asset in used],
    }
    if project.media is not None:
        metadata["editing"] = asdict(project.media)
    if project.voice != VoiceSettings():
        metadata["voice"] = asdict(project.voice)
    staged = _temporary_file(destination)
    try:
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED, compresslevel=4) as bundle:
            bundle.writestr("project.json", _encode_json(metadata))
            bundle.writestr("source.txt", project.source)
            for asset in used:
                bundle.write(asset_path(asset, directory), f"assets/{asset.stored_name}")
        staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)


def _keyed(items: list) -> dict:
    keyed = {item.id: item for item in items}
    if len(keyed) != len(items) or not all(isinstance(key, str) and key for key in keyed):
        raise ReplayError("工程记录的标识无效。")
    return keyed


def _check_asset_ref(ref: str, assets: dict[str, Asset], allowed: set[str]) -> None:
    if ref and (ref not in assets or assets[ref].extension not in allowed):
        raise ReplayError("工程中有素材缺失或格式不匹配。")


def _cue_from_data(item: dict, roles: dict[str, Role], scenes: dict[str, Scene],
                   message_count: int) -> Cue:
    initiative = item.get("initiative")
    battle = None
    if initiative is not None:
        turns = tuple(Turn(**turn) for turn in initiative["turns"])
        current = initiative["current"]
        if not isinstance(current, str) or not all(
            isinstance(turn.name, str) and isinstance(turn.score, str) for turn in turns
        ):
            raise ReplayError("工程中的战斗记录无效。")
        battle = Initiative(turns, current)
    cue = Cue(**{**item, "initiative": battle, "sources": tuple(item["sources"])})
    if cue.kind not in ("dialogue", "interlude", "scene", "battle_start", "battle_end"):
        raise ReplayError("工程中有未知类型的时间线项目。")
    if not all(isinstance(value, str) for value in (cue.text, cue.role, cue.stamp, cue.scene)):
        raise ReplayError("工程中的台词内容无效。")
    if (cue.role and cue.role not in roles) or (cue.kind == "scene" and cue.scene not in scenes):
        raise ReplayError("工程中的台词引用了不存在的角色或场景。")
    if not all(type(n) is int and 1 <= n <= message_count for n in cue.sources):
        raise ReplayError("工程中的原始消息编号无效。")
    if (type(cue.hidden) is not bool or not 0 <= cue.duration <= MAX_CUE_SECONDS
            or not math.isfinite(cue.duration)):
        raise ReplayError("工程中的台词状态无效。")
    return cue


def _read_editing(data: dict, assets: dict[str, Asset]) -> MediaEdit:
    tracks = [MediaTrack(**item) for item in data["tracks"]]
    by_track = _keyed(tracks)
    if (sum(track.kind == "background" for track in tracks) != 1
            or any(track.kind not in ("background", "music", "sound") or
                   not isinstance(track.name, str) for track in tracks)):
        raise ReplayError("工程中的剪辑轨道无效。")
    library = list(data["library"])
    for reference in library:
        if not isinstance(reference, str) or reference not in assets:
            raise ReplayError("工程素材库里有文件缺失。")
    clips = [MediaClip(**item) for item in data["clips"]]
    _keyed(clips)
    for clip in clips:
        if (clip.track not in by_track or clip.asset not in assets
                or not isinstance(clip.anchor, str) or type(clip.loop) is not bool):
            raise ReplayError("工程中的剪辑片段无效。")
        images = by_track[clip.track].kind == "background"
        _check_asset_ref(clip.asset, assets, IMAGE_EXTENSIONS if images else MUSIC_EXTENSIONS)
        _bounded_int(clip.length, 1, MAX_MEDIA_FRAME)
        _bounded_int(clip.start, 0, MAX_MEDIA_FRAME - clip.length)
        _bounded_int(clip.offset, -MAX_MEDIA_FRAME, MAX_MEDIA_FRAME)
        _bounded_int(clip.source_in, 0, MAX_MEDIA_FRAME)
        _bounded_int(clip.volume, 0, 100)
    bookmarks = [MediaBookmark(**item) for item in data["bookmarks"]]
    _keyed(bookmarks)
    for bookmark in bookmarks:
        if not isinstance(bookmark.name, str):
            raise ReplayError("工程中的书签无效。")
        _bounded_int(bookmark.frame, 0, MAX_MEDIA_FRAME)
    return MediaEdit(tracks, clips, list(dict.fromkeys(library)), bookmarks)


def _read_role_voice(data: dict, assets: dict[str, Asset]) -> RoleVoice:
    voice = RoleVoice(**data)
    if voice.mode not in ("mute", "ut", "ai"):
        raise ReplayError("工程中的角色配音模式无效。")
    if not all(isinstance(v, str) for v in (
        voice.ut_preset, voice.ut_asset, voice.provider, voice.voice, voice.ref_audio, voice.ref_text,
    )):
        raise ReplayError("工程中的角色配音参数无效。")
    _check_asset_ref(voice.ut_asset, assets, VOICE_EXTENSIONS)
    _bounded_int(voice.ut_pitch, -12, 12)
    _bounded_int(voice.ut_pace, 0, 2)
    _bounded_int(voice.rate, -50, 50)
    _bounded_int(voice.pitch, -50, 50)
    _bounded_int(voice.volume, 0, 100)
    return voice


def _read_cue_voice(data: dict, assets: dict[str, Asset]) -> CueVoice:
    cv = CueVoice(**data)
    if cv.mode not in ("inherit", "mute", "ut", "custom"):
        raise ReplayError("工程中的台词配音状态无效。")
    if not all(isinstance(v, str) for v in (cv.spoken_text, cv.asset, cv.fingerprint)):
        raise ReplayError("工程中的台词配音记录无效。")
    _check_asset_ref(cv.asset, assets, VOICE_EXTENSIONS)
    _bounded_int(cv.duration_frames, 0, MAX_MEDIA_FRAME)
    return cv


def _read_voice(data: dict, roles: dict[str, Role], cues: dict[str, Cue],
                assets: dict[str, Asset]) -> VoiceSettings:
    if (type(data.get("skip_dice", True)) is not bool
            or type(data.get("duck_music", True)) is not bool
            or type(data.get("auto_duration", True)) is not bool
            or data.get("bracket_mode", "skip") not in ("skip", "keep", "narrator")):
        raise ReplayError("工程中的声音设置无效。")
    duck_level = _bounded_int(data.get("duck_level", 35), 0, 100)
    gap_frames = _bounded_int(data.get("gap_frames", 9), 0, 300)
    narrator = _read_role_voice(data["narrator"], assets) if "narrator" in data else RoleVoice(voice="zh-CN-YunyangNeural")
    role_voices = {}
    for role_id, item in data.get("roles", {}).items():
        if role_id in roles:
            role_voices[role_id] = _read_role_voice(item, assets)
    cue_voices = {}
    for cue_id, item in data.get("cues", {}).items():
        if cue_id in cues:
            cue_voices[cue_id] = _read_cue_voice(item, assets)
    return VoiceSettings(
        skip_dice=data.get("skip_dice", True),
        bracket_mode=data.get("bracket_mode", "skip"),
        duck_music=data.get("duck_music", True),
        duck_level=duck_level,
        auto_duration=data.get("auto_duration", True),
        gap_frames=gap_frames,
        narrator=narrator,
        roles=role_voices,
        cues=cue_voices,
    )


def open_project(source: Path, directory: Path) -> Project:
    try:
        with zipfile.ZipFile(source) as bundle:
            metadata = json.loads(bundle.read("project.json"))
            version = metadata["version"]
            if version not in (LEGACY_PROJECT_VERSION, PROJECT_VERSION):
                raise ReplayError("这个工程的版本暂不支持。")
            title, source_name = metadata["title"], metadata["source_name"]
            if not isinstance(title, str) or not isinstance(source_name, str):
                raise ReplayError("工程名称或来源无效。")
            raw = bundle.read("source.txt")
            messages = parse_log(raw, source_name)
            roles = _keyed([Role(**item) for item in metadata["roles"]])
            scenes = _keyed([Scene(**item) for item in metadata["scenes"]])
            assets = _keyed([Asset(**item) for item in metadata["assets"]])
            for asset in assets.values():
                if (re.fullmatch(r"[0-9a-f]{64}", asset.id) is None
                        or asset.extension not in IMAGE_EXTENSIONS | MUSIC_EXTENSIONS | VOICE_EXTENSIONS
                        or not isinstance(asset.name, str)):
                    raise ReplayError("工程包中有无效的素材记录。")
            default_scene = metadata["default_scene"]
            if default_scene not in scenes:
                raise ReplayError("工程中缺少默认场景。")
            for role in roles.values():
                if not all(isinstance(value, str) for value in (role.name, role.account, role.portrait)):
                    raise ReplayError("工程中的角色信息无效。")
                _check_asset_ref(role.portrait, assets, IMAGE_EXTENSIONS)
            for scene in scenes.values():
                if not all(isinstance(value, str) for value in (scene.name, scene.background, scene.music)):
                    raise ReplayError("工程中的场景信息无效。")
                _bounded_int(scene.volume, 0, 100)
                _check_asset_ref(scene.background, assets, IMAGE_EXTENSIONS)
                _check_asset_ref(scene.music, assets, MUSIC_EXTENSIONS)
            cues = [_cue_from_data(item, roles, scenes, len(messages)) for item in metadata["cues"]]
            cue_map = _keyed(cues)
            template = _template_from_data(metadata["template"])
            speed = _bounded_int(metadata["reading_speed"], 1, 40)
            editing = _read_editing(metadata["editing"], assets) if version == PROJECT_VERSION else None
            voice = _read_voice(metadata["voice"], roles, cue_map, assets) if "voice" in metadata else VoiceSettings()
            for asset in assets.values():
                with bundle.open(f"assets/{asset.stored_name}") as entry:
                    with asset_path(asset, directory).open("wb") as target:
                        copyfileobj(entry, target)
    except (KeyError, IndexError, TypeError, UnicodeError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise ReplayError("工程包无法读取，或缺少原日志和素材。") from error
    return Project(title, source_name, raw, messages, roles, scenes, default_scene,
                   cues, template, speed, assets, editing, voice)
