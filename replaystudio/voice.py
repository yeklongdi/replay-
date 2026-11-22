from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import random
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from array import array
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QSettings

from .model import (
    FPS, Asset, Cue, CueVoice, Project, ReplayError, RoleVoice, VoiceSettings, new_id,
)
from .storage import import_bytes

SAMPLE_RATE = 48_000
SAMPLES_PER_FRAME = SAMPLE_RATE // FPS
API_SETTINGS_KEY = "voice/apis"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EDGE_VOICES: tuple[tuple[str, str], ...] = (
    ("zh-CN-YunxiNeural", "云希 · 男（阳光少年）"),
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女（温暖自然）"),
    ("zh-CN-YunjianNeural", "云健 · 男（激情叙事）"),
    ("zh-CN-YunyangNeural", "云扬 · 男（专业旁白）"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女（活泼少女）"),
    ("zh-CN-YunxiaNeural", "云夏 · 男（可爱少年）"),
    ("zh-CN-liaoning-XiaobeiNeural", "晓北 · 女（东北话 · 幽默）"),
    ("zh-CN-shaanxi-XiaoniNeural", "晓妮 · 女（陕西话 · 明快）"),
    ("zh-HK-HiuMaanNeural", "晓曼 · 女（粤语 · 亲切）"),
    ("zh-HK-HiuGaaiNeural", "晓佳 · 女（粤语 · 温和）"),
    ("zh-HK-WanLungNeural", "云龙 · 男（粤语 · 沉稳）"),
    ("zh-TW-HsiaoChenNeural", "晓臻 · 女（台湾腔 · 亲切）"),
    ("zh-TW-HsiaoYuNeural", "晓雨 · 女（台湾腔 · 温柔）"),
    ("zh-TW-YunJheNeural", "云哲 · 男（台湾腔 · 自然）"),
)
EDGE_VOICE_LABELS = dict(EDGE_VOICES)

UT_PRESETS: tuple[tuple[str, str], ...] = (
    ("crisp", "清脆对话（标准 ADV）"),
    ("deep", "沉稳低鸣（大叔 / 守卫）"),
    ("bright", "轻快哔哔（少年 / 活泼）"),
    ("pixel", "复古像素（UT 经典风）"),
    ("typewriter", "机械打字机（旁白 / 档案）"),
    ("custom", "自定义本地短音效"),
)
UT_PRESET_LABELS = dict(UT_PRESETS)
UT_PACES: tuple[str, ...] = ("慢", "标准", "快")
UT_PACE_STEPS: tuple[float, ...] = (0.105, 0.072, 0.048)

PROTOCOL_LABELS: dict[str, str] = {
    "gptsovits": "GPT-SoVITS (api_v2)",
    "openai": "OpenAI 兼容 (/v1/audio/speech)",
    "custom": "通用 HTTP JSON 模板",
}

DICE_COMMAND = re.compile(r"^[ \t]*[.。][a-zA-Z]+(?:\b|[\d\s+\-*/]|$)")
CHECK_RESULT = re.compile(r"^[ \t]*<[^>\n]+>[ \t]*(?:对|进行|的)")
INITIATIVE_TEXT = re.compile(r"^[ \t]*当前先攻列表")
URL_RE = re.compile(r"https?://\S+")
BRACKET_RE = re.compile(r"（[^（）\n]*）|\([^()\n]*\)|【[^【】\n]*】|\[[^\[\]\n]*\]")
UNCLOSED_BRACKET_RE = re.compile(r"[（(【\[][^\n]*")
SPEAKABLE_CHAR = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")


@dataclass(slots=True)
class VoiceApiConfig:
    id: str
    name: str
    protocol: str = "openai"  # gptsovits | openai | custom
    url: str = "http://127.0.0.1:8080/v1/audio/speech"
    api_key: str = ""
    model_or_lang: str = "tts-1"
    voices: str = "alloy, echo, nova, shimmer"
    ref_audio: str = ""
    ref_text: str = ""
    auth_header: str = "Authorization"
    body_template: str = '{"text": "{text}", "speaker": "{voice}", "speed": {speed}}'
    timeout: int = 30
    response_format: str = "auto"  # auto | wav | mp3

    def voice_choices(self) -> list[str]:
        return [item.strip() for item in self.voices.split(",") if item.strip()]


def voice_settings_store() -> QSettings:
    return QSettings("ReplayStudio", "ReplayStudio")


def load_api_configs() -> list[VoiceApiConfig]:
    raw = voice_settings_store().value(API_SETTINGS_KEY, "", type=str)
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
        result = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            valid = {key: item[key] for key in VoiceApiConfig.__slots__ if key in item}
            cfg = VoiceApiConfig(**valid)
            if cfg.protocol not in PROTOCOL_LABELS:
                cfg.protocol = "custom"
            cfg.timeout = max(5, min(300, int(cfg.timeout)))
            result.append(cfg)
        return result
    except (ValueError, TypeError):
        return []


def save_api_configs(configs: list[VoiceApiConfig]) -> None:
    payload = json.dumps([asdict(item) for item in configs], ensure_ascii=False)
    voice_settings_store().setValue(API_SETTINGS_KEY, payload)


def api_config_map(configs: list[VoiceApiConfig] | None = None) -> dict[str, VoiceApiConfig]:
    items = load_api_configs() if configs is None else configs
    return {item.id: item for item in items}


def new_api_config(protocol: str = "gptsovits", name: str = "") -> VoiceApiConfig:
    if protocol == "gptsovits":
        return VoiceApiConfig(
            id=new_id(),
            name=name or "本机 GPT-SoVITS",
            protocol="gptsovits",
            url="http://127.0.0.1:9880/tts",
            model_or_lang="zh",
            voices="",
            response_format="wav",
        )
    if protocol == "custom":
        return VoiceApiConfig(
            id=new_id(),
            name=name or "通用 HTTP 语音接口",
            protocol="custom",
            url="http://127.0.0.1:50000/tts",
            model_or_lang="",
            voices="default",
            body_template='{"text": "{text}", "speaker": "{voice}", "speed": {speed}}',
            response_format="auto",
        )
    return VoiceApiConfig(
        id=new_id(),
        name=name or "OpenAI 兼容接口",
        protocol="openai",
        url="http://127.0.0.1:8080/v1/audio/speech",
        model_or_lang="tts-1",
        voices="alloy, echo, nova, shimmer",
        response_format="mp3",
    )


def role_voice_for(settings: VoiceSettings, role_id: str) -> RoleVoice:
    if not role_id:
        return settings.narrator
    return settings.roles.get(role_id, RoleVoice())


def ensure_role_voice(settings: VoiceSettings, role_id: str) -> RoleVoice:
    if not role_id:
        return settings.narrator
    if role_id not in settings.roles:
        settings.roles[role_id] = RoleVoice()
    return settings.roles[role_id]


def is_dice_or_system_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if DICE_COMMAND.match(stripped) or CHECK_RESULT.match(stripped) or INITIATIVE_TEXT.match(stripped):
        return True
    return SPEAKABLE_CHAR.search(stripped) is None


def _normalize_segment(text: str) -> str:
    cleaned = URL_RE.sub("", text)
    cleaned = re.sub(r"[ \t\r\n]+", " ", cleaned).strip()
    return cleaned if SPEAKABLE_CHAR.search(cleaned) else ""


def prepare_spoken_segments(
    text: str, settings: VoiceSettings, cue_voice: CueVoice | None = None,
) -> tuple[tuple[str, str], ...]:
    if cue_voice and cue_voice.spoken_text.strip():
        part = _normalize_segment(cue_voice.spoken_text)
        return (("role", part),) if part else ()
    if settings.skip_dice and is_dice_or_system_text(text):
        return ()
    raw = URL_RE.sub("", text).strip()
    if not raw:
        return ()
    if settings.bracket_mode == "skip":
        no_closed = BRACKET_RE.sub(" ", raw)
        no_unclosed = UNCLOSED_BRACKET_RE.sub(" ", no_closed)
        stripped = _normalize_segment(no_unclosed)
        return (("role", stripped),) if stripped else ()
    if settings.bracket_mode == "keep":
        plain = re.sub(r"[（）()【】\[\]]", " ", raw)
        cleaned = _normalize_segment(plain)
        return (("role", cleaned),) if cleaned else ()
    # narrator mode: split both closed and unclosed brackets out to narrator voice
    segments: list[tuple[str, str]] = []
    for line in raw.splitlines():
        pos = 0
        for match in BRACKET_RE.finditer(line):
            before = line[pos:match.start()]
            if unclosed := UNCLOSED_BRACKET_RE.search(before):
                pre = _normalize_segment(before[:unclosed.start()])
                if pre:
                    segments.append(("role", pre))
                nar = _normalize_segment(re.sub(r"[（）()【】\[\]]", " ", before[unclosed.start():]))
                if nar:
                    segments.append(("narrator", nar))
            else:
                pre = _normalize_segment(before)
                if pre:
                    segments.append(("role", pre))
            inner = _normalize_segment(match.group(0)[1:-1])
            if inner:
                segments.append(("narrator", inner))
            pos = match.end()
        tail = line[pos:]
        if unclosed := UNCLOSED_BRACKET_RE.search(tail):
            pre = _normalize_segment(tail[:unclosed.start()])
            if pre:
                segments.append(("role", pre))
            nar = _normalize_segment(re.sub(r"[（）()【】\[\]]", " ", tail[unclosed.start():]))
            if nar:
                segments.append(("narrator", nar))
        else:
            pre = _normalize_segment(tail)
            if pre:
                segments.append(("role", pre))
    merged: list[tuple[str, str]] = []
    for kind, part in segments:
        if merged and merged[-1][0] == kind:
            merged[-1] = (kind, merged[-1][1] + "，" + part)
        else:
            merged.append((kind, part))
    return tuple(merged)


def effective_cue_mode(project: Project, cue: Cue) -> str:
    if not cue.is_text or cue.hidden or not cue.text.strip():
        return "mute"
    settings = project.voice
    cv = settings.cues.get(cue.id)
    if cv and cv.mode == "mute":
        return "mute"
    if cv and cv.mode == "custom":
        return "custom"
    rv = role_voice_for(settings, cue.role)
    mode = "ut" if (cv and cv.mode == "ut") else rv.mode
    if mode == "mute":
        return "mute"
    if not prepare_spoken_segments(cue.text, settings, cv):
        return "mute"
    return mode


def voice_fingerprint(
    segments: tuple[tuple[str, str], ...],
    role_voice: RoleVoice,
    narrator_voice: RoleVoice,
    apis: dict[str, VoiceApiConfig],
) -> str:
    def _spec(rv: RoleVoice) -> dict:
        api = apis.get(rv.provider)
        return {
            "provider": rv.provider,
            "voice": rv.voice,
            "rate": rv.rate,
            "pitch": rv.pitch,
            "ref_audio": rv.ref_audio,
            "ref_text": rv.ref_text,
            "url": api.url if api else "",
            "protocol": api.protocol if api else "edge",
            "model": api.model_or_lang if api else "",
            "default_ref": api.ref_audio if api else "",
            "body": api.body_template if api else "",
        }

    payload = {
        "segments": segments,
        "role": _spec(role_voice),
        "narrator": _spec(narrator_voice) if any(kind == "narrator" for kind, _ in segments) else None,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def ut_cache_key(text: str, role_voice: RoleVoice, length_frames: int) -> str:
    payload = (
        f"ut|{role_voice.ut_preset}|{role_voice.ut_asset}|{role_voice.ut_pitch}|"
        f"{role_voice.ut_pace}|{length_frames}|{text}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _synth_blip(preset: str, semitones: int, jitter: float) -> list[int]:
    factor = (2.0 ** (semitones / 12.0)) * (1.0 + jitter)
    if preset == "deep":
        base_hz, dur = 165.0 * factor, 0.055
    elif preset == "bright":
        base_hz, dur = 680.0 * factor, 0.042
    elif preset == "pixel":
        base_hz, dur = 440.0 * factor, 0.048
    elif preset == "typewriter":
        base_hz, dur = 920.0 * factor, 0.028
    else:  # crisp
        base_hz, dur = 410.0 * factor, 0.046

    count = max(64, int(SAMPLE_RATE * dur))
    attack = max(16, int(SAMPLE_RATE * 0.004))
    release = max(48, int(count * 0.55))
    rng = random.Random(int(base_hz * 100) ^ count)
    out: list[int] = []
    phase = 0.0
    for i in range(count):
        env = 1.0
        if i < attack:
            env = i / attack
        elif i > count - release:
            env = max(0.0, (count - i) / release) ** 1.6
        if preset == "pixel":
            freq = base_hz * (1.0 + 0.06 * (1.0 - i / count))
            phase += 2.0 * math.pi * freq / SAMPLE_RATE
            val = 0.65 if math.sin(phase) >= 0 else -0.65
            val = val * 0.62 + 0.38 * math.sin(phase)
        elif preset == "deep":
            freq = base_hz * (1.0 - 0.14 * (i / count))
            phase += 2.0 * math.pi * freq / SAMPLE_RATE
            val = 0.72 * math.sin(phase) + 0.28 * math.sin(2.0 * phase)
        elif preset == "bright":
            freq = base_hz * (1.0 + 0.12 * (1.0 - i / count))
            phase += 2.0 * math.pi * freq / SAMPLE_RATE
            val = 0.78 * math.sin(phase) + 0.22 * math.sin(3.0 * phase)
        elif preset == "typewriter":
            phase += 2.0 * math.pi * base_hz / SAMPLE_RATE
            noise = rng.uniform(-1.0, 1.0) * math.exp(-i / (SAMPLE_RATE * 0.006))
            click = math.sin(phase) * math.exp(-i / (SAMPLE_RATE * 0.010))
            val = 0.58 * noise + 0.42 * click
        else:  # crisp
            freq = base_hz * (1.0 - 0.04 * (i / count))
            phase += 2.0 * math.pi * freq / SAMPLE_RATE
            tri = (2.0 / math.pi) * math.asin(math.sin(phase))
            val = 0.65 * math.sin(phase) + 0.35 * tri
        out.append(max(-32767, min(32767, round(val * env * 13_500))))
    return out


@lru_cache(maxsize=512)
def _cached_blip_stereo(preset: str, semitones: int, variant: int) -> array:
    jitter = (-0.022, 0.0, 0.022)[variant % 3]
    mono = _synth_blip(preset, semitones, jitter)
    stereo = array("h")
    for sample in mono:
        stereo.append(sample)
        stereo.append(sample)
    return stereo


def _resample_custom_blip(pcm_path: Path, semitones: int) -> array:
    raw = pcm_path.read_bytes()
    if len(raw) < 4:
        return _cached_blip_stereo("crisp", semitones, 1)
    samples = array("h")
    samples.frombytes(raw[: min(len(raw), SAMPLE_RATE * 4 // 2)])  # cap at 0.5s
    mono = [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)]
    if not mono:
        return _cached_blip_stereo("crisp", semitones, 1)
    factor = 2.0 ** (semitones / 12.0)
    out_len = max(64, min(int(SAMPLE_RATE * 0.12), int(len(mono) / factor)))
    out = array("h")
    for i in range(out_len):
        src = i * factor
        idx = int(src)
        frac = src - idx
        if idx + 1 < len(mono):
            val = mono[idx] * (1.0 - frac) + mono[idx + 1] * frac
        else:
            val = mono[-1]
        clamped = max(-32767, min(32767, round(val * 0.85)))
        out.append(clamped)
        out.append(clamped)
    return out


def render_ut_pcm(
    text: str,
    role_voice: RoleVoice,
    duration_frames: int,
    custom_pcm_path: Path | None = None,
) -> bytes:
    total_samples = max(SAMPLES_PER_FRAME, duration_frames * SAMPLES_PER_FRAME)
    pace_idx = max(0, min(len(UT_PACE_STEPS) - 1, role_voice.ut_pace))
    step_samples = max(640, int(SAMPLE_RATE * UT_PACE_STEPS[pace_idx]))
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return bytes(total_samples * 4)

    max_speak_samples = min(total_samples, max(step_samples * 2, int(total_samples * 0.82)))
    max_blips = max(1, min(len(chars), max_speak_samples // step_samples))

    custom_blip = None
    if role_voice.ut_preset == "custom" and custom_pcm_path and custom_pcm_path.is_file():
        custom_blip = _resample_custom_blip(custom_pcm_path, role_voice.ut_pitch)

    out = array("h", [0]) * (total_samples * 2)
    cursor = int(SAMPLE_RATE * 0.025)
    for idx in range(max_blips):
        ch = chars[idx]
        if ch in "，。！？、；：,.!?;:…—-～~":
            cursor += step_samples
            if cursor >= max_speak_samples:
                break
            continue
        blip = custom_blip if custom_blip is not None else _cached_blip_stereo(
            role_voice.ut_preset, role_voice.ut_pitch, (ord(ch) + idx) % 3,
        )
        blip_frames = len(blip) // 2
        usable = min(blip_frames, total_samples - cursor)
        if usable > 0:
            start_i = cursor * 2
            end_i = start_i + usable * 2
            # Non-overlapping step_samples > blip duration, direct slice copy is C-speed!
            if step_samples >= blip_frames:
                out[start_i:end_i] = blip[: usable * 2]
            else:
                for k in range(usable * 2):
                    out[start_i + k] = max(-32767, min(32767, out[start_i + k] + blip[k]))
        cursor += step_samples
        if cursor >= max_speak_samples:
            break
    return out.tobytes()


def detect_audio_extension(data: bytes, hint: str = "auto") -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data[:4] == b"OggS":
        return ".ogg"
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return ".mp3"
    if hint in ("wav", ".wav"):
        return ".wav"
    if hint in ("ogg", ".ogg"):
        return ".ogg"
    return ".mp3"


def synthesize_edge_bytes(text: str, voice: str, rate: int = 0, pitch: int = 0) -> bytes:
    try:
        import edge_tts
    except ImportError as error:
        raise ReplayError("未安装 edge-tts，请先运行 pip install edge-tts。") from error

    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    if not cleaned:
        raise ReplayError("无可朗读文本。")
    rate_str = f"{rate:+d}%"
    pitch_str = f"{pitch:+d}Hz"

    async def _run() -> bytes:
        comm = edge_tts.Communicate(cleaned, voice=voice, rate=rate_str, pitch=pitch_str)
        chunks = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                chunks.extend(chunk["data"])
        return bytes(chunks)

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            data = asyncio.run(_run())
            if data:
                return data
            raise ReplayError("Edge-TTS 未返回有效音频。")
        except Exception as error:
            last_error = error
            logging.warning(
                "Edge-TTS attempt %d failed (voice=%s rate=%s pitch=%s text=%r): %s",
                attempt + 1, voice, rate_str, pitch_str, cleaned[:40], error,
            )
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    raise ReplayError(f"Edge-TTS 合成失败（音色 {voice}）：{last_error}")


def _extract_audio_response(raw: bytes, content_type: str, format_hint: str) -> tuple[bytes, str]:
    if "application/json" in content_type.lower() or raw.lstrip().startswith(b"{"):
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReplayError("自定义接口返回了无法解析的 JSON。") from error
        for key in ("audio", "data", "audio_base64", "wav"):
            if isinstance(parsed.get(key), str) and parsed[key]:
                decoded = base64.b64decode(parsed[key])
                return decoded, detect_audio_extension(decoded, format_hint)
        message = parsed.get("message") or parsed.get("error") or parsed.get("detail") or str(parsed)[:200]
        raise ReplayError(f"语音接口返回错误：{message}")
    if len(raw) < 64:
        raise ReplayError("语音接口返回的音频数据为空或过短。")
    return raw, detect_audio_extension(raw, format_hint)


def synthesize_api_bytes(
    config: VoiceApiConfig, text: str, role_voice: RoleVoice,
) -> tuple[bytes, str]:
    url = config.url.strip()
    if not url:
        raise ReplayError(f"接口「{config.name}」尚未填写 URL。")
    speed = max(0.5, min(2.0, round(1.0 + role_voice.rate / 100.0, 2)))
    headers = {"Content-Type": "application/json", "Accept": "*/*"}
    if config.api_key.strip():
        header_name = config.auth_header.strip() or "Authorization"
        token = config.api_key.strip()
        if header_name.lower() == "authorization" and " " not in token:
            token = f"Bearer {token}"
        headers[header_name] = token

    if config.protocol == "gptsovits":
        ref_audio = role_voice.ref_audio.strip() or config.ref_audio.strip()
        ref_text = role_voice.ref_text.strip() or config.ref_text.strip()
        lang = config.model_or_lang.strip() or "zh"
        payload = {
            "text": text,
            "text_lang": lang,
            "ref_audio_path": ref_audio,
            "prompt_text": ref_text,
            "prompt_lang": lang,
            "speed_factor": speed,
            "media_type": "wav" if config.response_format != "mp3" else "mp3",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    elif config.protocol == "openai":
        voice = role_voice.voice.strip() or (config.voice_choices()[0] if config.voice_choices() else "alloy")
        payload = {
            "model": config.model_or_lang.strip() or "tts-1",
            "input": text,
            "voice": voice,
            "speed": speed,
            "response_format": "wav" if config.response_format == "wav" else "mp3",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    else:
        template = config.body_template.strip() or '{"text": "{text}", "speaker": "{voice}", "speed": {speed}}'
        voice = role_voice.voice.strip() or (config.voice_choices()[0] if config.voice_choices() else "default")
        rendered = (
            template
            .replace("{text}", json.dumps(text, ensure_ascii=False)[1:-1])
            .replace("{voice}", json.dumps(voice, ensure_ascii=False)[1:-1])
            .replace("{speed}", str(speed))
            .replace("{pitch}", str(role_voice.pitch))
        )
        body = rendered.encode("utf-8")

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=max(5, config.timeout)) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:260]
        raise ReplayError(f"接口「{config.name}」返回 HTTP {error.code}：{detail}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ReplayError(f"无法连接接口「{config.name}」：{error}") from error
    return _extract_audio_response(raw, content_type, config.response_format)


def synthesize_segment_bytes(
    text: str, role_voice: RoleVoice, apis: dict[str, VoiceApiConfig],
) -> tuple[bytes, str]:
    if role_voice.provider == "edge":
        voice = role_voice.voice if role_voice.voice in EDGE_VOICE_LABELS else EDGE_VOICES[0][0]
        return synthesize_edge_bytes(text, voice, role_voice.rate, role_voice.pitch), ".mp3"
    config = apis.get(role_voice.provider)
    if config is None:
        raise ReplayError("该角色绑定的自定义语音接口不存在，请先在「API 接口」里配置。")
    return synthesize_api_bytes(config, text, role_voice)


def _concat_segments_to_mp3(parts: list[tuple[bytes, str]], ffmpeg: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="replay-voice-") as folder:
        work = Path(folder)
        inputs: list[str] = []
        filter_parts: list[str] = []
        for idx, (data, ext) in enumerate(parts):
            clip = work / f"part-{idx}{ext}"
            clip.write_bytes(data)
            inputs.extend(["-i", str(clip)])
            filter_parts.append(f"[{idx}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=mono[a{idx}]")
        gap = f"aevalsrc=0:d=0.12:s={SAMPLE_RATE}:c=mono[gap]"
        concat_inputs = []
        for idx in range(len(parts)):
            if idx:
                concat_inputs.append("[gap]")
            concat_inputs.append(f"[a{idx}]")
        graph = ";".join([
            *filter_parts,
            *([] if len(parts) < 2 else [gap]),
            f"{''.join(concat_inputs)}concat=n={len(concat_inputs)}:v=0:a=1[out]",
        ])
        target = work / "merged.mp3"
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *inputs,
             "-filter_complex", graph, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "64k", str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NO_WINDOW,
        )
        if proc.returncode:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            raise ReplayError(f"合并配音片段失败：{detail[-240:]}")
        return target.read_bytes()


def synthesize_cue_audio(
    project: Project,
    cue: Cue,
    directory: Path,
    cache,
    apis: dict[str, VoiceApiConfig],
) -> tuple[Asset, str, int]:
    from .audio import find_ffmpeg

    settings = project.voice
    cv = settings.cues.get(cue.id)
    rv = role_voice_for(settings, cue.role)
    segments = prepare_spoken_segments(cue.text, settings, cv)
    if not segments:
        raise ReplayError("该条台词按当前朗读规则无可朗读内容。")
    fp = voice_fingerprint(segments, rv, settings.narrator, apis)
    if cv and cv.fingerprint == fp and cv.asset in project.assets and cv.duration_frames > 0:
        return project.assets[cv.asset], fp, cv.duration_frames

    parts: list[tuple[bytes, str]] = []
    for kind, seg_text in segments:
        voice_cfg = settings.narrator if kind == "narrator" else rv
        if voice_cfg.mode == "mute" and kind == "narrator":
            voice_cfg = rv
        parts.append(synthesize_segment_bytes(seg_text, voice_cfg, apis))

    if len(parts) == 1:
        data, ext = parts[0]
    else:
        data = _concat_segments_to_mp3(parts, find_ffmpeg())
        ext = ".mp3"

    asset = import_bytes(project, data, f"voice-{cue.id}{ext}", ext, directory)
    frames = max(1, cache.frame_length(asset.id))
    return asset, fp, frames


def test_api_connection(config: VoiceApiConfig) -> tuple[bytes, str, int]:
    started = time.perf_counter()
    sample_rv = RoleVoice(
        mode="ai",
        provider=config.id,
        voice=(config.voice_choices()[0] if config.voice_choices() else "alloy"),
        ref_audio=config.ref_audio,
        ref_text=config.ref_text,
    )
    data, ext = synthesize_api_bytes(config, "你好，语音接口测试正常。", sample_rv)
    latency_ms = max(1, round((time.perf_counter() - started) * 1000))
    return data, ext, latency_ms


def decode_bytes_to_pcm(data: bytes, ext: str) -> bytes:
    from .audio import find_ffmpeg

    with tempfile.TemporaryDirectory(prefix="replay-audition-") as folder:
        src = Path(folder) / f"audition{ext}"
        src.write_bytes(data)
        proc = subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src),
             "-map", "0:a:0", "-vn", "-ar", str(SAMPLE_RATE), "-ac", "2",
             "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW,
        )
        if proc.returncode or not proc.stdout:
            raise ReplayError("无法解码返回的试听音频。")
        return proc.stdout
