from __future__ import annotations

import re
import zipfile
import zlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from email import message_from_bytes, policy as email_policy
from html.parser import HTMLParser
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
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"

MESSAGE_HEADER = re.compile(
    r"^(?P<name>[^\n]+?)\((?P<account>[0-9]+|[A-Za-z][A-Za-z0-9_-]*:[A-Za-z0-9_.-]+)\)[ \t]+"
    r"(?P<date>[0-9]{4}[/-][0-9]{1,2}[/-][0-9]{1,2})[ \t]+"
    r"(?P<time>[0-9]{1,2}:[0-9]{2}:[0-9]{2})[ \t]*$",
    re.MULTILINE,
)
INLINE_ACCOUNT = re.compile(
    r"^(?P<name>.+?)\((?P<account>[0-9]+|[A-Za-z][A-Za-z0-9_-]*:[A-Za-z0-9_.-]+)\)$"
)
BBCODE_MULTILINE_HEADER = re.compile(
    r"^[ \t]*\[color=[^\]]+\][ \t]*<(?P<name>[^<>\n]+)>[ \t]*\[/color\][ \t]*\[color=[^\]]+\]",
    re.MULTILINE | re.IGNORECASE,
)
BBCODE_TIMED_HEADER = re.compile(
    r"^[ \t]*\[color=[^\]]+\][ \t]*(?:(?P<date>[0-9]{4}[/-][0-9]{1,2}[/-][0-9]{1,2})[ \t]+)?"
    r"(?P<time>[0-9]{1,2}:[0-9]{2}:[0-9]{2})[ \t]*\[/color\][ \t]*"
    r"\[color=[^\]]+\][ \t]*<(?P<name>[^<>\n]+)>[：:]?",
    re.MULTILINE | re.IGNORECASE,
)
BBCODE_TIMELESS_HEADER = re.compile(
    r"^[ \t]*\[color=[^\]]+\][ \t]*<(?P<name>[^<>\n]+)>[：:]?",
    re.MULTILINE | re.IGNORECASE,
)
INLINE_TIMED_HEADER = re.compile(
    r"^[ \t]*(?:(?P<date>[0-9]{4}[/-][0-9]{1,2}[/-][0-9]{1,2})[ \t]+)?"
    r"(?P<time>[0-9]{1,2}:[0-9]{2}:[0-9]{2})[ \t]*"
    r"<(?P<name>[^<>\n]+)>[：:]",
    re.MULTILINE,
)
INLINE_TIMELESS_HEADER = re.compile(
    r"^<(?P<name>[^<>\n]+)>[：:]",
    re.MULTILINE,
)
ECHO_MESSAGE_HEADER = re.compile(
    r"^[ \t]*(?:#[ \t]+)?\[(?P<name>[^\[\]\n]+)\][：:]",
    re.MULTILINE,
)
BBCODE_CLOSING_TAG = re.compile(r"[ \t]*\[/color\][ \t]*$", re.IGNORECASE)
ECHO_VOICE_MARK = re.compile(r"\{\*\}[ \t]*$")
ECHO_KP_SUFFIX = re.compile(r"[ \t]*[,，][ \t]*KP$", re.IGNORECASE)
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


def _split_name_account(raw_name: str) -> tuple[str, str]:
    cleaned = raw_name.strip()
    if match := INLINE_ACCOUNT.fullmatch(cleaned):
        if name := match["name"].strip():
            return name, match["account"]
    return cleaned, ""


def _format_stamp(date_text: str | None, time_text: str | None, index: int) -> tuple[str, bool]:
    try:
        if date_text and time_text:
            normalized_date = date_text.replace("-", "/")
            stamp = datetime.strptime(f"{normalized_date} {time_text}", "%Y/%m/%d %H:%M:%S").isoformat(sep=" ")
            return stamp, True
        if time_text:
            return datetime.strptime(time_text, "%H:%M:%S").strftime("%H:%M:%S"), False
    except ValueError as error:
        raise ReplayError(f"第 {index + 1} 条消息的日期或时间无效。") from error
    return "", False


def _clean_bbcode_content(raw_content: str, raw_name: str, *, strip_continuation_prefix: bool) -> str:
    text = BBCODE_CLOSING_TAG.sub("", raw_content.strip()).strip()
    if strip_continuation_prefix and "\n" in text:
        prefix_pattern = re.compile(rf"^[\s\u00a0\u2002/:[\]]*<{re.escape(raw_name)}>[：:]?")
        lines = text.splitlines()
        cleaned = [lines[0]]
        for line in lines[1:]:
            cleaned.append(prefix_pattern.sub("", line).lstrip())
        text = BBCODE_CLOSING_TAG.sub("", "\n".join(cleaned)).strip()
    return text


def _clean_echo_content(raw_content: str) -> str:
    lines = [ECHO_VOICE_MARK.sub("", line).rstrip() for line in raw_content.splitlines()]
    return "\n".join(lines).strip()


def _format_table_row(speaker_cell: str, body_cell: str) -> str:
    speaker = " ".join(speaker_cell.split())
    body = body_cell.strip()
    if INLINE_TIMED_HEADER.match(speaker) or INLINE_TIMELESS_HEADER.match(speaker):
        return f"{speaker} {body}".rstrip()
    speaker = speaker.rstrip(":：").strip()
    if not (speaker.startswith("<") and speaker.endswith(">")):
        speaker = f"<{speaker}>"
    return f"{speaker}: {body}" if body else f"{speaker}:"


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    text_tag = f"{WORD_NAMESPACE}t"
    whitespace = {
        f"{WORD_NAMESPACE}br": "\n",
        f"{WORD_NAMESPACE}cr": "\n",
        f"{WORD_NAMESPACE}tab": "\t",
    }
    return "".join(
        (node.text or "") if node.tag == text_tag else whitespace.get(node.tag, "")
        for node in paragraph.iter()
    )


def _docx_log_source(source: bytes) -> bytes:
    try:
        with zipfile.ZipFile(BytesIO(source)) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, zlib.error, LookupError, RuntimeError, ElementTree.ParseError) as error:
        raise ReplayError("无法读取 DOCX 日志。请检查文件是否为可打开的 Word 文档。") from error

    body = document.find(f"{WORD_NAMESPACE}body")
    if body is None:
        raise ReplayError("DOCX 中没有可读取的正文。")
    p_tag = f"{WORD_NAMESPACE}p"
    tbl_tag = f"{WORD_NAMESPACE}tbl"
    tr_tag = f"{WORD_NAMESPACE}tr"
    tc_tag = f"{WORD_NAMESPACE}tc"
    paragraphs = []
    for child in body:
        if child.tag == p_tag:
            paragraphs.append(_docx_paragraph_text(child))
        elif child.tag == tbl_tag:
            for row in child.findall(f".//{tr_tag}"):
                cells = [
                    "\n".join(_docx_paragraph_text(p) for p in cell.findall(f".//{p_tag}"))
                    for cell in row.findall(tc_tag)
                ]
                if len(cells) >= 2 and cells[0].strip():
                    paragraphs.append(_format_table_row(
                        cells[0], "\n".join(part for part in cells[1:] if part.strip()),
                    ))
                else:
                    paragraphs.extend(part for part in cells if part.strip())
    return "\n".join(paragraphs).encode("utf-8")


_VOID_HTML_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_BLOCK_BREAK_TAGS = frozenset({"p", "div", "li", "blockquote"})


class _StoryPainterHtmlExtractor(HTMLParser):
    """提取海豹着色器带图 DOC、对话 DOC 或网页 HTML 中的结构化对话行。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.span_entries: list[str] = []
        self.table_entries: list[str] = []
        self.fallback_lines: list[str] = []
        self._stack: list[tuple[str, str | None]] = []
        self._active_span_field: str | None = None
        self._span_time = ""
        self._span_nickname = ""
        self._span_message: list[str] = []
        self._in_row = False
        self._row_cells: list[str] = []
        self._in_cell = False
        self._cell_buf: list[str] = []
        self._fallback_buf: list[str] = []
        self._skip_depth = 0

    def _flush_span_entry(self) -> None:
        time_text = " ".join(self._span_time.split())
        nickname = " ".join(self._span_nickname.split())
        message = "".join(self._span_message).strip()
        self._span_time = ""
        self._span_nickname = ""
        self._span_message = []
        if not nickname and not message:
            return
        if nickname and not nickname.endswith((":", "：")):
            nickname = f"{nickname}:"
        parts = [part for part in (time_text, nickname, message) if part]
        self.span_entries.append(" ".join(parts))

    def _append_break(self, buf: list[str]) -> None:
        if buf and not buf[-1].endswith("\n"):
            buf.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = dict(attrs)
        if tag in ("script", "style", "head"):
            self._skip_depth += 1
            self._stack.append((tag, None))
            return
        if self._skip_depth:
            if tag not in _VOID_HTML_TAGS:
                self._stack.append((tag, None))
            return

        if tag == "img":
            src = (attr_map.get("src") or "").strip()
            token = f"[图:{src}]" if src else "[图:无可用链接]"
            if self._in_cell:
                self._cell_buf.append(token)
            elif self._active_span_field == "message":
                self._span_message.append(token)
            elif not self._in_row:
                self._fallback_buf.append(token)
            return

        if tag == "br":
            if self._in_cell:
                self._cell_buf.append("\n")
            elif self._active_span_field == "message":
                self._span_message.append("\n")
            elif not self._in_row:
                self._fallback_buf.append("\n")
            return

        if tag in _VOID_HTML_TAGS:
            return

        span_role = None
        if not self._in_row and tag == "span":
            classes = set((attr_map.get("class") or "").split())
            if "_time" in classes:
                if self._span_nickname or self._span_message:
                    self._flush_span_entry()
                span_role = "time"
                self._active_span_field = "time"
            elif "_nickname" in classes:
                if self._span_nickname:
                    self._flush_span_entry()
                span_role = "nickname"
                self._active_span_field = "nickname"
            elif "_message" in classes:
                span_role = "message"
                self._active_span_field = "message"
        self._stack.append((tag, span_role))

        if tag == "tr":
            self._in_row = True
            self._row_cells = []
            return
        if self._in_row and tag in ("td", "th"):
            self._in_cell = True
            self._cell_buf = []
            return

        if tag in _BLOCK_BREAK_TAGS:
            if self._in_cell:
                self._append_break(self._cell_buf)
            elif self._active_span_field == "message":
                self._append_break(self._span_message)
            elif not self._in_row:
                self._append_break(self._fallback_buf)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_HTML_TAGS:
            return
        popped_roles: list[str | None] = []
        while self._stack:
            top_tag, role = self._stack.pop()
            if top_tag in ("script", "style", "head") and self._skip_depth:
                self._skip_depth -= 1
            popped_roles.append(role)
            if top_tag == tag:
                break
        if self._skip_depth:
            return

        if tag in ("td", "th") and self._in_cell:
            self._row_cells.append("".join(self._cell_buf).strip())
            self._cell_buf = []
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if len(self._row_cells) >= 2 and self._row_cells[0].strip():
                self.table_entries.append(_format_table_row(
                    self._row_cells[0], "\n".join(part for part in self._row_cells[1:] if part.strip()),
                ))
            self._row_cells = []
            self._in_row = False
            self._in_cell = False

        if "message" in popped_roles:
            self._flush_span_entry()
        self._active_span_field = next(
            (role for _, role in reversed(self._stack) if role is not None), None,
        )

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if self._in_cell:
            self._cell_buf.append(data)
        elif self._active_span_field == "time":
            self._span_time += data
        elif self._active_span_field == "nickname":
            self._span_nickname += data
        elif self._active_span_field == "message":
            self._span_message.append(data)
        elif not self._in_row:
            self._fallback_buf.append(data)

    def lines(self) -> list[str]:
        if self._span_nickname or self._span_message:
            self._flush_span_entry()
        if self.span_entries:
            return self.span_entries
        if self.table_entries:
            return self.table_entries
        return [line.strip() for line in "".join(self._fallback_buf).splitlines() if line.strip()]


def _looks_like_doc_html(source: bytes) -> bool:
    head = source[:4096].lower()
    return any(marker in head for marker in (
        b"mime-version:", b"multipart/related", b"<!doctype html", b"<html", b"<table", b"_nickname",
    ))


def _decode_html_part(part) -> str:
    charset = part.get_content_charset() or "utf-8"
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    for encoding in (charset, "utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _doc_log_source(source: bytes) -> bytes:
    if source.startswith(OLE2_MAGIC):
        raise ReplayError("暂不支持旧版二进制 .doc 文件。请用 Word 另存为 .docx，或从海豹着色器重新导出。")
    if source.startswith(ZIP_MAGIC):
        return _docx_log_source(source)

    html_text = ""
    message = message_from_bytes(source, policy=email_policy.default)
    for part in message.walk():
        if part.get_content_type() == "text/html":
            html_text = _decode_html_part(part)
            if html_text.strip():
                break

    if not html_text.strip() and _looks_like_doc_html(source):
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                html_text = source.decode(encoding)
                break
            except UnicodeDecodeError:
                continue

    if not html_text.strip():
        raise ReplayError("无法读取 DOC 日志。请检查文件是否为海豹着色器导出的带图或对话 DOC 文档。")

    extractor = _StoryPainterHtmlExtractor()
    extractor.feed(html_text)
    extractor.close()
    lines = extractor.lines()
    if not lines:
        raise ReplayError("DOC 中没有可读取的正文。")
    return "\n".join(lines).encode("utf-8")


def parse_log(source: bytes, filename: str = "") -> tuple[Message, ...]:
    lower = filename.lower()
    docx = lower.endswith(".docx")
    doc = lower.endswith(".doc")
    try:
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        if docx:
            raise ReplayError("工程中的 DOCX 日志正文无法读取。") from error
        if doc:
            raise ReplayError("工程中的 DOC 日志正文无法读取。") from error
        raise ReplayError("请选择 UTF-8 编码的 TXT 日志。") from error
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    strategies = (
        ("raw", MESSAGE_HEADER),
        ("bbcode_multi", BBCODE_MULTILINE_HEADER),
        ("bbcode_single", BBCODE_TIMED_HEADER),
        ("bbcode_single", BBCODE_TIMELESS_HEADER),
        ("inline", INLINE_TIMED_HEADER),
        ("inline", INLINE_TIMELESS_HEADER),
        ("echo", ECHO_MESSAGE_HEADER),
    )
    kind = ""
    headers: list[re.Match[str]] = []
    for candidate_kind, pattern in strategies:
        found = list(pattern.finditer(text))
        if found:
            kind, headers = candidate_kind, found
            break

    if not headers:
        if docx:
            raise ReplayError("DOCX 中没有找到消息头。跑团日志应为「时:分:秒 <昵称>: 内容」等海豹着色器导出格式。")
        if doc:
            raise ReplayError("DOC 中没有找到消息头。请确认文件为海豹着色器导出的带图或对话 DOC 日志。")
        raise ReplayError("没有找到可识别的跑团日志消息头。支持原始 TXT、海豹着色器预览、论坛代码或回声工坊导出格式。")

    parsed: list[tuple[str, str, str, bool, str]] = []
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        raw_slice = text[header.end():end]
        groups = header.groupdict()
        raw_name = groups["name"]
        if kind == "raw":
            name = raw_name.strip()
            account = groups["account"]
            stamp, has_date = _format_stamp(groups["date"], groups["time"], i)
            content = raw_slice.strip()
        elif kind == "bbcode_multi":
            name, account = _split_name_account(raw_name)
            stamp, has_date = "", False
            content = _clean_bbcode_content(raw_slice, raw_name, strip_continuation_prefix=False)
        elif kind == "bbcode_single":
            name, account = _split_name_account(raw_name)
            stamp, has_date = _format_stamp(groups.get("date"), groups.get("time"), i)
            content = _clean_bbcode_content(raw_slice, raw_name, strip_continuation_prefix=True)
        elif kind == "inline":
            name, account = _split_name_account(raw_name)
            stamp, has_date = _format_stamp(groups.get("date"), groups.get("time"), i)
            content = raw_slice.strip()
        else:
            speaker = ECHO_KP_SUFFIX.sub("", raw_name.strip()).strip()
            name, account = _split_name_account(speaker)
            stamp, has_date = "", False
            content = _clean_echo_content(raw_slice)
        parsed.append((name, account, stamp, has_date, content))

    use_sessions = not all(has_date for _, _, _, has_date, _ in parsed)
    messages = []
    session = 1 if use_sessions else 0
    for i, (name, account, stamp, _, content) in enumerate(parsed):
        messages.append(Message(i + 1, name, account, stamp, content, session))
        if use_sessions and DOCX_SESSION_END.fullmatch(content.partition("\n")[0].rstrip()):
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
    lower = filename.lower()
    if lower.endswith(".docx"):
        source = (
            _doc_log_source(source)
            if not source.startswith(ZIP_MAGIC) and _looks_like_doc_html(source)
            else _docx_log_source(source)
        )
    elif lower.endswith(".doc"):
        source = _doc_log_source(source)
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
