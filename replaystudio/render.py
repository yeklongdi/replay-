from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from functools import lru_cache
from itertools import pairwise
from math import ceil
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QImage, QImageReader, QLinearGradient, QPainter,
    QPen, QTextLayout, QTextOption,
)

from .media_timing import MediaSchedule, legacy_media, resolve_media
from .model import (
    DEFAULT_FONT, DEFAULT_SCENE_NAME, FPS, HEIGHT, WIDTH, Box, Initiative, MediaEdit, Project, RoleVoice,
)
from .storage import asset_path
from .voice import effective_cue_mode, prepare_spoken_segments, role_voice_for, ut_cache_key

BODY_PADDING_X = 48
BODY_PADDING_Y = 26


@dataclass(slots=True, frozen=True)
class Page:
    cue_id: str
    cue_kind: str
    role: str
    scene: str
    scene_start: int
    index: int
    page_count: int
    lines: tuple[str, ...]
    initiative: Initiative | None
    start: int
    end: int


@dataclass(slots=True, frozen=True)
class VisualSpan:
    start: int
    end: int
    page: Page | None
    background: str


@dataclass(slots=True, frozen=True)
class VoiceSpan:
    id: str
    cue_id: str
    role_id: str
    mode: str  # ai | custom | ut
    start: int
    length: int
    asset: str
    volume: int
    label: str
    text: str = ""
    ut_key: str = ""
    role_voice: RoleVoice | None = None

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass(slots=True)
class Timeline:
    pages: list[Page]
    starts: list[int]
    cue_frames: dict[str, int]
    length: int
    media: MediaSchedule = field(default_factory=MediaSchedule)
    dialogue_starts: dict[str, int] = field(default_factory=dict)
    visual_starts: list[int] = field(default_factory=list)
    voices: tuple[VoiceSpan, ...] = ()
    duck_spans: tuple[tuple[int, int], ...] = ()

    @property
    def dialogue_end(self) -> int:
        return self.pages[-1].end if self.pages else 0

    def index_at(self, frame: int) -> int:
        return max(0, bisect_right(self.starts, frame) - 1)

    def page_at(self, frame: int) -> Page | None:
        return self.pages[self.index_at(frame)] if 0 <= frame < self.dialogue_end else None

    def between(self, start: int, end: int) -> list[Page]:
        if start >= self.dialogue_end:
            return []
        first = self.index_at(start)
        stop = self.index_at(end - 1) + 1
        return self.pages[first:stop]

    def visuals_between(self, start: int, end: int) -> list[VisualSpan]:
        boundaries = [start, *(at for at in self.visual_starts if start < at < end), end]
        return [VisualSpan(left, right, self.page_at(left), self.media.background_at(left))
                for left, right in pairwise(boundaries)]


def font(size: int, bold: bool = False) -> QFont:
    result = QFont(DEFAULT_FONT)
    result.setPixelSize(size)
    if bold:
        result.setWeight(QFont.Weight.DemiBold)
    return result


def body_line_step(size: int) -> int:
    return ceil(QFontMetricsF(font(size)).lineSpacing() * 1.14)


@lru_cache(maxsize=16000)
def paginate(text: str, size: int, width: int, height: int) -> tuple[tuple[str, ...], ...]:
    current_font = font(size)
    line_width = max(1, width - 2 * BODY_PADDING_X)
    maximum_lines = max(1, (height - 2 * BODY_PADDING_Y) // body_line_step(size))
    lines = []
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    for paragraph in text.replace("\t", "    ").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        qt_layout = QTextLayout(paragraph, current_font)
        qt_layout.setTextOption(option)
        utf16 = paragraph.encode("utf-16-le")
        qt_layout.beginLayout()
        while (line := qt_layout.createLine()).isValid():
            line.setLineWidth(line_width)
            offset = line.textStart()
            end = offset + line.textLength()
            lines.append(utf16[offset * 2:end * 2].decode("utf-16-le"))
        qt_layout.endLayout()
    return tuple(tuple(lines[i:i + maximum_lines]) for i in range(0, len(lines), maximum_lines))


def _page_lengths(groups: tuple[tuple[str, ...], ...], speed: int, override: float) -> list[int]:
    weights = [max(1, sum(not ch.isspace() for line in lines for ch in line)) for lines in groups]
    if not override:
        return [max(2 * FPS, ceil(FPS * length / speed)) for length in weights]
    available = max(0, round(override * FPS) - len(groups))
    total_weight = sum(weights)
    frames = []
    prefix = 0
    previous = 0
    for weight in weights:
        prefix += weight
        cut = round(available * prefix / total_weight)
        frames.append(1 + cut - previous)
        previous = cut
    return frames


def _merge_spans(spans: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    if not spans:
        return ()
    ordered = sorted(spans)
    merged = [ordered[0]]
    for left, right in ordered[1:]:
        prev_left, prev_right = merged[-1]
        if left <= prev_right:
            merged[-1] = (prev_left, max(prev_right, right))
        else:
            merged.append((left, right))
    return tuple(merged)


def build_timeline(project: Project) -> Timeline:
    scene = project.default_scene
    scene_start = 0
    battle_active = False
    snapshot = None
    pages, cue_frames = [], {}
    voices: list[VoiceSpan] = []
    frame = 0
    box = project.template.dialogue
    voice_cfg = project.voice
    for cue in project.cues:
        if cue.hidden:
            continue
        if cue.kind == "scene":
            scene, scene_start = cue.scene, frame
        elif cue.kind in ("battle_start", "battle_end"):
            battle_active = cue.kind == "battle_start"
            snapshot = None
        elif cue.is_text and cue.text.strip():
            if cue.initiative is not None:
                if not cue.initiative.turns and cue.initiative.current and battle_active and snapshot:
                    snapshot = Initiative(snapshot.turns, cue.initiative.current)
                else:
                    snapshot = cue.initiative if cue.initiative.turns else None
            current = snapshot if battle_active or cue.initiative is not None else None
            lines = paginate(cue.text, project.template.body_size, box.width, box.height)

            vmode = effective_cue_mode(project, cue)
            rv = role_voice_for(voice_cfg, cue.role)
            cv = voice_cfg.cues.get(cue.id)
            effective_duration = cue.duration
            has_audio_asset = (
                vmode in ("ai", "custom")
                and cv is not None
                and cv.asset in project.assets
                and cv.duration_frames > 0
            )
            if not effective_duration and voice_cfg.auto_duration and has_audio_asset:
                effective_duration = max(1.0, (cv.duration_frames + voice_cfg.gap_frames) / FPS)

            lengths = _page_lengths(lines, project.reading_speed, effective_duration)
            cue_start = frame
            cue_frames[cue.id] = frame
            for i, (group, length) in enumerate(zip(lines, lengths)):
                pages.append(Page(cue.id, cue.kind, cue.role, scene, scene_start,
                                  i, len(lines), group, current, frame, frame + length))
                frame += length
            cue_length = frame - cue_start
            if cue_length > 0 and rv.volume > 0:
                actor = project.roles.get(cue.role)
                speaker = actor.name if actor else "旁白"
                if has_audio_asset:
                    vlen = min(cue_length, cv.duration_frames)
                    tag = "干声" if vmode == "custom" else ("API" if rv.provider != "edge" else "AI")
                    voices.append(VoiceSpan(
                        id=f"voice-{cue.id}",
                        cue_id=cue.id,
                        role_id=cue.role,
                        mode=vmode,
                        start=cue_start,
                        length=max(1, vlen),
                        asset=cv.asset,
                        volume=rv.volume,
                        label=f"{tag} · {speaker}",
                    ))
                elif vmode == "ut":
                    segments = prepare_spoken_segments(cue.text, voice_cfg, cv)
                    ut_text = " ".join(part for _, part in segments)
                    if ut_text:
                        ut_key = ut_cache_key(ut_text, rv, cue_length)
                        voices.append(VoiceSpan(
                            id=f"voice-{cue.id}",
                            cue_id=cue.id,
                            role_id=cue.role,
                            mode="ut",
                            start=cue_start,
                            length=cue_length,
                            asset=f"ut:{ut_key}",
                            volume=rv.volume,
                            label=f"UT · {speaker}",
                            text=ut_text,
                            ut_key=ut_key,
                            role_voice=rv,
                        ))
            continue
        if not cue.is_text:
            cue_frames[cue.id] = frame
    dialogue_starts = {page.cue_id: page.start for page in pages if not page.index}
    edit = project.media if project.media is not None else legacy_media(project, pages, cue_frames)
    schedule = resolve_media(edit, dialogue_starts, frame)
    starts = [page.start for page in pages]
    visual_starts = sorted({0, frame, *starts, *(span.start for span in schedule.backgrounds),
                            *(span.end for span in schedule.backgrounds)} - {schedule.length})
    duck_spans = _merge_spans([(v.start, v.end) for v in voices]) if voice_cfg.duck_music else ()
    return Timeline(pages, starts, cue_frames, schedule.length, schedule, dialogue_starts, visual_starts,
                    tuple(voices), duck_spans)


def initialize_media(project: Project) -> None:
    if project.media is not None:
        return
    first = project.scenes[project.default_scene]
    if (first.name != DEFAULT_SCENE_NAME
            or any(scene.background or scene.music for scene in project.scenes.values())
            or any(cue.kind == "scene" for cue in project.cues)):
        old = build_timeline(project)
        project.media = legacy_media(project, old.pages, old.cue_frames)
        project.cues[:] = [cue for cue in project.cues if cue.kind != "scene"]
        project.scenes = {first.id: replace(first, background="", music="")}
    else:
        project.media = MediaEdit()


def as_rect(box: Box) -> QRectF:
    return QRectF(box.x, box.y, box.width, box.height)


class Images:
    def __init__(self, directory: Path):
        self.directory = directory
        self._cache: OrderedDict[str, QImage] = OrderedDict()

    def get(self, project: Project, ref: str) -> QImage:
        if ref in self._cache:
            self._cache.move_to_end(ref)
            return self._cache[ref]
        asset = project.assets.get(ref)
        if not asset:
            return QImage()
        reader = QImageReader(str(asset_path(asset, self.directory)))
        reader.setAutoTransform(True)
        if (source := reader.size()).isValid() and (source.width() > 2560 or source.height() > 1440):
            reader.setScaledSize(source.scaled(QSize(2560, 1440), Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        self._cache[ref] = image
        if len(self._cache) > 12:
            self._cache.popitem(last=False)
        return image


def _put_image(painter: QPainter, image: QImage, region: QRectF, cover: bool) -> None:
    sx, sy = region.width() / image.width(), region.height() / image.height()
    ratio = max(sx, sy) if cover else min(sx, sy)
    width, height = image.width() * ratio, image.height() * ratio
    top = region.center().y() - height / 2 if cover else region.bottom() - height
    painter.save()
    painter.setClipRect(region, Qt.ClipOperation.IntersectClip)
    painter.drawImage(QRectF(region.center().x() - width / 2, top, width, height), image)
    painter.restore()


def _background(painter: QPainter) -> None:
    gradient = QLinearGradient(0, 0, WIDTH, HEIGHT)
    gradient.setColorAt(0, QColor("#263B39"))
    gradient.setColorAt(0.52, QColor("#1E3030"))
    gradient.setColorAt(1, QColor("#101B22"))
    painter.fillRect(QRectF(0, 0, WIDTH, HEIGHT), gradient)
    painter.setPen(QPen(QColor(169, 207, 190, 16), 1))
    for x in range(64, WIDTH, 96):
        painter.drawLine(x, 0, x, HEIGHT)
    for y in range(54, HEIGHT, 96):
        painter.drawLine(0, y, WIDTH, y)


def _placeholder(painter: QPainter, portrait: QRectF, name: str) -> None:
    region = portrait.adjusted(portrait.width() * .10, portrait.height() * .26,
                               -portrait.width() * .10, -portrait.height() * .09)
    painter.setBrush(QColor(70, 116, 101, 75))
    painter.setPen(QPen(QColor(169, 207, 190, 104), 2))
    painter.drawRoundedRect(region, 20, 20)
    painter.setPen(QColor("#CCE3D4"))
    painter.setFont(font(max(20, round(min(region.width(), region.height()) * .43)), True))
    initial = next((char for char in name if char.isalnum()), "◇")
    painter.drawText(region, Qt.AlignmentFlag.AlignCenter, initial)


def _turn_panel(painter: QPainter, initiative: Initiative) -> None:
    turns = initiative.turns
    columns = 2 if len(turns) > 13 else 1
    rows = ceil(len(turns) / columns)
    width = 474 * columns
    line_height = max(16, min(53, 535 // max(1, rows)))
    left = WIDTH - width - 66
    panel = QRectF(left, 74, width, 86 + line_height * rows + 14)
    painter.setBrush(QColor("#152326"))
    painter.setPen(QPen(QColor("#4B6A64"), 2))
    painter.drawRoundedRect(panel, 22, 22)
    painter.setPen(QColor("#CAE7D0"))
    painter.setFont(font(34, True))
    painter.drawText(QPointF(panel.left() + 24, panel.top() + 50), "先攻顺序")
    for index, turn in enumerate(turns):
        top = panel.top() + 75 + (index % rows) * line_height
        column_left = panel.left() + (index // rows) * (width / columns)
        active = bool(initiative.current) and turn.name == initiative.current
        row = QRectF(column_left + 14, top, width / columns - 28, line_height - 3)
        if active:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#B8E0C3"))
            painter.drawRoundedRect(row, 8, 8)
        painter.setPen(QColor("#162927" if active else "#E3EBE3"))
        f = font(min(31, round(line_height * .66)), active)
        painter.setFont(f)
        metrics = QFontMetricsF(f)
        score_space = min(132, int(metrics.horizontalAdvance(turn.score)) + 10) if turn.score else 0
        name = f"{index + 1:02}  {turn.name}"
        text_rect = row.adjusted(10, 0, -10, 0)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(name, Qt.TextElideMode.ElideRight, text_rect.width() - score_space))
        if turn.score:
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             metrics.elidedText(turn.score, Qt.TextElideMode.ElideRight, score_space))


def draw_page(painter: QPainter, project: Project, page: Page | None, images: Images,
              background_ref: str | None = None) -> None:
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    _background(painter)
    template = project.template
    if background_ref is None and page is not None:
        background_ref = project.scenes[page.scene].background
    background = images.get(project, background_ref or "")
    if not background.isNull() and template.background_opacity:
        painter.save()
        painter.setOpacity(template.background_opacity / 100)
        _put_image(painter, background, QRectF(0, 0, WIDTH, HEIGHT), cover=True)
        painter.restore()

    if page is None:
        return

    role = project.roles.get(page.role)
    if role and page.cue_kind == "dialogue":
        portrait = as_rect(template.portrait)
        image = images.get(project, role.portrait)
        if image.isNull():
            _placeholder(painter, portrait, role.name)
        else:
            _put_image(painter, image, portrait, cover=False)
        label = as_rect(template.speaker)
        f = font(template.speaker_size, True)
        painter.setFont(f)
        metrics = QFontMetricsF(f)
        name = metrics.elidedText(role.name, Qt.TextElideMode.ElideRight, label.width() - 54)
        label.setWidth(min(label.width(), metrics.horizontalAdvance(name) + 54))
        painter.setBrush(QColor("#162927"))
        painter.setPen(QPen(QColor("#58746A"), 2))
        painter.drawRoundedRect(label, 13, 13)
        painter.setPen(QColor(template.speaker_color))
        painter.drawText(label, Qt.AlignmentFlag.AlignCenter, name)

    if page.initiative and page.initiative.turns:
        _turn_panel(painter, page.initiative)

    dialogue = as_rect(template.dialogue)
    painter.save()
    painter.setClipRect(dialogue, Qt.ClipOperation.IntersectClip)
    fill = QColor(template.box_color)
    fill.setAlphaF(template.box_opacity / 100)
    painter.setBrush(fill)
    painter.setPen(QPen(QColor(169, 207, 190, round(template.box_opacity * 1.12)), 2))
    painter.drawRoundedRect(dialogue, 20, 20)
    painter.setPen(QColor(template.body_color))
    body_font = font(template.body_size)
    painter.setFont(body_font)
    metrics = QFontMetricsF(body_font)
    step = body_line_step(template.body_size)
    top = dialogue.center().y() - len(page.lines) * step / 2
    for index, line in enumerate(page.lines):
        x = dialogue.left() + BODY_PADDING_X
        if page.cue_kind == "interlude":
            x = dialogue.center().x() - metrics.horizontalAdvance(line) / 2
        baseline = top + index * step + (step - metrics.height()) / 2 + metrics.ascent()
        painter.drawText(QPointF(x, baseline), line)
    painter.restore()


def render_image(project: Project, page: Page | None, images: Images,
                 background: str | None = None) -> QImage:
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    try:
        draw_page(painter, project, page, images, background)
    finally:
        painter.end()
    return image
