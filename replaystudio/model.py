from __future__ import annotations

import re
import zipfile
import zlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from io import BytesIO
from uuid import uuid4
from xml.etree import ElementTree

WIDTH = 1920
HEIGHT = 1080
FPS = 30
MAX_CUE_SECONDS = (2**31 - 1) // FPS
MAX_MEDIA_FRAME = 2**31 - 1
DEFAULT_FONT = "Noto Sans SC"
DEFAULT_SCENE_NAME = "默认场景"

MESSAGE_HEADER = re.compile(
    r"^(?P<name>[^\n]+?)\((?P<account>[0-9]+)\)[ \t]+"
    r"(?P<date>[0-9]{4}/[0-9]{1,2}/[0-9]{1,2})[ \t]+"
    r"(?P<time>[0-9]{1,2}:[0-9]{2}:[0-9]{2})[ \t]*$",
    re.MULTILINE,
)
DOCX_MESSAGE_HEADER = re.compile(
    r"^[ \t]*(?P<time>[0-9]{1,2}:[0-9]{2}:[0-9]{2})[ \t]+"
    r"<(?P<name>[^<>\n]+)>[：:]",
    re.MULTILINE,
)
DOCX_SESSION_END = re.compile(r"\.log[ \t]+off", re.IGNORECASE)
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
INITIATIVE_HEADER = re.compile(r"^[ \t]*当前先攻列表(?:为)?[：:][ \t]*$", re.MULTILINE)
TURN_LINE = re.compile(r"^[ \t]*[0-9]+[.．、][ \t]*(.+?)[：:][ \t]*(.*)$")
ACTIVE_LINE = re.compile(r"^[ \t]*(?:当前回合|当前行动者)[：:][ \t]*(\S[^\n]*)$", re.MULTILINE)


class ReplayError(ValueError):
    pass


def new_id() -> str:
    return uuid4().hex


@dataclass(slots=True, frozen=True)
class Message:
    number: int
    name: str
    account: str
    stamp: str
    text: str
    session: int = 0


@dataclass(slots=True)
class Role:
    id: str
    name: str
    account: str = ""  # 日志中的发言人标识，手动添加的角色留空
    portrait: str = ""


@dataclass(slots=True)
class Scene:
    id: str
    name: str
    background: str = ""
    music: str = ""
    volume: int = 70


@dataclass(slots=True, frozen=True)
class Turn:
    name: str
    score: str = ""


@dataclass(slots=True, frozen=True)
class Initiative:
    turns: tuple[Turn, ...] = ()
    current: str = ""


@dataclass(slots=True)
class Cue:
    id: str
    kind: str = "dialogue"
    text: str = ""
    role: str = ""
    stamp: str = ""
    sources: tuple[int, ...] = ()
    hidden: bool = False
    duration: float = 0.0
    scene: str = ""
    initiative: Initiative | None = None

    @property
    def is_text(self) -> bool:
        return self.kind in ("dialogue", "interlude")


@dataclass(slots=True, frozen=True)
class Box:
    x: int
    y: int
    width: int
    height: int

    def bounded(self, minimum_width: int, minimum_height: int) -> Box:
        width = max(minimum_width, min(WIDTH, self.width))
        height = max(minimum_height, min(HEIGHT, self.height))
        return Box(max(0, min(WIDTH - width, self.x)), max(0, min(HEIGHT - height, self.y)),
                   width, height)


TEMPLATE_MINIMUMS = {
    "portrait": (96, 112),
    "speaker": (144, 100),
    "dialogue": (360, 150),
}


@dataclass(slots=True)
class Template:
    name: str = "夜幕"
    portrait: Box = Box(106, 116, 518, 714)
    speaker: Box = Box(151, 665, 600, 100)
    dialogue: Box = Box(132, 758, 1656, 244)
    body_size: int = 43
    speaker_size: int = 36
    body_color: str = "#F4EFE1"
    speaker_color: str = "#C4EBCF"
    box_color: str = "#10191C"
    box_opacity: int = 89
    background_opacity: int = 100


@dataclass(slots=True, frozen=True)
class Asset:
    id: str
    name: str
    extension: str

    @property
    def stored_name(self) -> str:
        return self.id + self.extension


@dataclass(slots=True)
class MediaTrack:
    id: str
    name: str
    kind: str


@dataclass(slots=True, frozen=True)
class MediaClip:
    id: str
    track: str
    asset: str
    start: int
    length: int
    anchor: str = ""
    offset: int = 0
    source_in: int = 0
    volume: int = 100
    loop: bool = False

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass(slots=True)
class MediaBookmark:
    id: str
    name: str
    frame: int


def default_tracks() -> list[MediaTrack]:
    return [MediaTrack("background", "背景", "background"),
            MediaTrack("music", "配乐", "music"), MediaTrack("sound", "音效", "sound")]


@dataclass(slots=True)
class MediaEdit:
    tracks: list[MediaTrack] = field(default_factory=default_tracks)
    clips: list[MediaClip] = field(default_factory=list)
    library: list[str] = field(default_factory=list)
    bookmarks: list[MediaBookmark] = field(default_factory=list)


@dataclass(slots=True)
class RoleVoice:
    mode: str = "mute"  # mute | ut | ai
    ut_preset: str = "crisp"
    ut_asset: str = ""
    ut_pitch: int = 0
    ut_pace: int = 1
    provider: str = "edge"
    voice: str = "zh-CN-YunxiNeural"
    ref_audio: str = ""
    ref_text: str = ""
    rate: int = 0
    pitch: int = 0
    volume: int = 90


@dataclass(slots=True)
class CueVoice:
    mode: str = "inherit"  # inherit | mute | ut | custom
    spoken_text: str = ""
    asset: str = ""
    fingerprint: str = ""
    duration_frames: int = 0


@dataclass(slots=True)
class VoiceSettings:
    skip_dice: bool = True
    bracket_mode: str = "skip"  # skip | keep | narrator
    duck_music: bool = True
    duck_level: int = 35
    auto_duration: bool = True
    gap_frames: int = 9
    narrator: RoleVoice = field(default_factory=lambda: RoleVoice(voice="zh-CN-YunyangNeural"))
    roles: dict[str, RoleVoice] = field(default_factory=dict)
    cues: dict[str, CueVoice] = field(default_factory=dict)


@dataclass(slots=True)
class Project:
    title: str
    source_name: str
    source: bytes
    messages: tuple[Message, ...]
    roles: dict[str, Role]
    scenes: dict[str, Scene]
    default_scene: str
    cues: list[Cue]
    template: Template = field(default_factory=Template)
    reading_speed: int = 8
    assets: dict[str, Asset] = field(default_factory=dict)
    media: MediaEdit | None = None
    voice: VoiceSettings = field(default_factory=VoiceSettings)

    def insert(self, index: int, kind: str, text: str = "", role: str = "",
               scene: str = "") -> Cue:
        cue = Cue(new_id(), kind=kind, text=text, role=role, scene=scene)
        self.cues.insert(index, cue)
        return cue

    def split(self, index: int, offset: int) -> Cue:
        cue = self.cues[index]
        left, right = cue.text[:offset].rstrip(), cue.text[offset:].lstrip()
        if not cue.is_text or not left or not right:
            raise ReplayError("请把光标放在要拆开的台词中间。")
        other = replace(cue, id=new_id(), text=right, initiative=None)
        if cue.duration:
            share = len(left) / (len(left) + len(right))
            other.duration = max(1 / FPS, cue.duration * (1 - share))
            cue.duration = max(1 / FPS, cue.duration * share)
        cue.text = left
        if existing := self.voice.cues.get(cue.id):
            if existing.mode != "custom":
                existing.asset = ""
                existing.fingerprint = ""
                existing.duration_frames = 0
        self.cues.insert(index + 1, other)
        return other

    def merge(self, indices: list[int], role: str) -> Cue:
        indices = sorted(set(indices))
        if len(indices) < 2 or indices != list(range(indices[0], indices[-1] + 1)):
            raise ReplayError("请选择至少两条相邻台词。")
        parts = [self.cues[i] for i in indices]
        first = parts[0]
        if not all(part.is_text and part.kind == first.kind for part in parts):
            raise ReplayError("请选择相邻的同类型台词或过场。")
        duration = sum(part.duration for part in parts) if all(p.duration for p in parts) else 0
        if duration > MAX_CUE_SECONDS:
            raise ReplayError("这些台词合并后超出可播放时长。")
        first.text = "\n".join(part.text for part in parts)
        first.role = role
        first.sources = tuple(dict.fromkeys(n for part in parts for n in part.sources))
        first.hidden = any(part.hidden for part in parts)
        first.duration = duration
        first.initiative = next((part.initiative for part in reversed(parts) if part.initiative), None)
        if existing := self.voice.cues.get(first.id):
            if existing.mode != "custom":
                existing.asset = ""
                existing.fingerprint = ""
                existing.duration_frames = 0
        for part in parts[1:]:
            self.voice.cues.pop(part.id, None)
        del self.cues[indices[0] + 1:indices[-1] + 1]
        return first

    def move(self, ids: set[str], direction: int) -> None:
        positions = range(1, len(self.cues)) if direction < 0 else range(len(self.cues) - 2, -1, -1)
        for i in positions:
            j = i + direction
            if self.cues[i].id in ids and self.cues[j].id not in ids:
                self.cues[i], self.cues[j] = self.cues[j], self.cues[i]

    def remove(self, indices: set[int]) -> None:
        scenes_to_remove = {self.cues[index].scene for index in indices
                            if self.cues[index].kind == "scene"}
        removed_ids = {self.cues[index].id for index in indices}
        self.cues[:] = [cue for index, cue in enumerate(self.cues) if index not in indices]
        for cue_id in removed_ids:
            self.voice.cues.pop(cue_id, None)
        if scenes_to_remove:
            linked_scenes = {cue.scene for cue in self.cues if cue.kind == "scene"}
            linked_scenes.add(self.default_scene)
            for scene in scenes_to_remove - linked_scenes:
                self.scenes.pop(scene, None)

    def used_assets(self) -> set[str]:
        refs = {role.portrait for role in self.roles.values()}
        if self.media is None:
            refs.update(scene.background for scene in self.scenes.values())
            refs.update(scene.music for scene in self.scenes.values())
        else:
            refs.update(self.media.library)
            refs.update(clip.asset for clip in self.media.clips)
        refs.add(self.voice.narrator.ut_asset)
        refs.update(rv.ut_asset for rv in self.voice.roles.values())
        refs.update(cv.asset for cv in self.voice.cues.values())
        refs.discard("")
        return refs


def _docx_log_source(source: bytes) -> bytes:
    try:
        with zipfile.ZipFile(BytesIO(source)) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, zlib.error, LookupError, RuntimeError, ElementTree.ParseError) as error:
        raise ReplayError("无法读取 DOCX 日志。请检查文件是否为可打开的 Word 文档。") from error

    body = document.find(f"{WORD_NAMESPACE}body")
    if body is None:
        raise ReplayError("DOCX 中没有可读取的正文。")
    text_tag = f"{WORD_NAMESPACE}t"
    whitespace = {
        f"{WORD_NAMESPACE}br": "\n",
        f"{WORD_NAMESPACE}cr": "\n",
        f"{WORD_NAMESPACE}tab": "\t",
    }
    paragraphs = []
    for paragraph in body.findall(f"{WORD_NAMESPACE}p"):
        paragraphs.append("".join(
            (node.text or "") if node.tag == text_tag else whitespace.get(node.tag, "")
            for node in paragraph.iter()
        ))
    return "\n".join(paragraphs).encode("utf-8")


def parse_log(source: bytes, filename: str = "") -> tuple[Message, ...]:
    docx = filename.lower().endswith(".docx")
    try:
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        if docx:
            raise ReplayError("工程中的 DOCX 日志正文无法读取。") from error
        raise ReplayError("请选择 UTF-8 编码的 TXT 日志。") from error
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    pattern = DOCX_MESSAGE_HEADER if docx else MESSAGE_HEADER
    headers = list(pattern.finditer(text))
    if not headers:
        if docx:
            raise ReplayError("DOCX 中没有找到消息头。跑团日志应为「时:分:秒 <昵称>: 内容」。")
        raise ReplayError("没有找到样例格式的消息头。需要「昵称(账号) 年/月/日 时:分:秒」。")
    messages = []
    session = 1 if docx else 0
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        try:
            if docx:
                stamp = datetime.strptime(header["time"], "%H:%M:%S").strftime("%H:%M:%S")
            else:
                stamp = datetime.strptime(
                    f"{header['date']} {header['time']}", "%Y/%m/%d %H:%M:%S"
                ).isoformat(sep=" ")
        except ValueError as error:
            raise ReplayError(f"第 {i + 1} 条消息的日期或时间无效。") from error
        account = "" if docx else header["account"]
        content = text[header.end():end].strip()
        messages.append(Message(i + 1, header["name"].strip(), account, stamp, content, session))
        if docx and DOCX_SESSION_END.fullmatch(content.partition("\n")[0].rstrip()):
            session += 1
    return tuple(messages)


def extract_initiative(text: str) -> Initiative | None:
    header = INITIATIVE_HEADER.search(text)
    active = ACTIVE_LINE.search(text)
    turns = []
    if header:
        for line in text[header.end():].lstrip("\n").splitlines():
            entry = TURN_LINE.fullmatch(line)
            if not entry:
                break
            turns.append(Turn(entry[1].strip(), entry[2].strip()))
    cleared = header and text[header.end():].lstrip().startswith("- 没有找到任何单位")
    if turns or active or cleared:
        current = active[1].strip().strip("<>") if active else ""
        return Initiative(tuple(turns), current)
    return None


def project_from_log(source: bytes, filename: str, title: str) -> Project:
    if filename.lower().endswith(".docx"):
        source = _docx_log_source(source)
    messages = parse_log(source, filename)
    senders: dict[str, Role] = {}
    cues: list[Cue] = []
    for message in messages:
        sender = message.account or message.name
        role = senders.get(sender)
        if role is None:
            role = Role(new_id(), message.name or "未命名角色", sender)
            senders[sender] = role
        cues.append(Cue(f"m{message.number}", text=message.text, role=role.id,
                        stamp=message.stamp, sources=(message.number,),
                        initiative=extract_initiative(message.text)))
    scene = Scene(new_id(), DEFAULT_SCENE_NAME)
    return Project(title, filename, source, messages, {role.id: role for role in senders.values()},
                   {scene.id: scene}, scene.id, cues)
