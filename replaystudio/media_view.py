from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractScrollArea, QMenu

from .model import FPS, MAX_MEDIA_FRAME, MediaClip, MediaTrack, Project
from .render import Timeline
from .widgets import timecode

ASSET_MIME = "application/x-replay-media-asset"
LABEL_WIDTH = 132
RULER_HEIGHT = 28
BOOKMARK_HEIGHT = 25
DIALOGUE_HEIGHT = 28
TRACK_TOP = RULER_HEIGHT + BOOKMARK_HEIGHT + DIALOGUE_HEIGHT
LANE_HEIGHT = 35
TRACK_COLORS = {
    "background": "#395a50", "music": "#365568", "sound": "#69583e", "voice": "#2f5b55",
}
VOICE_TRACK_ID = "__voice__"


@dataclass(slots=True)
class TrackRow:
    track: MediaTrack
    top: int
    height: int
    clips: list[tuple[MediaClip, int]]


class TrackView(QAbstractScrollArea):
    seek = Signal(int)
    selection = Signal(str)
    drop_asset = Signal(str, str, int)
    adjust_clip = Signal(str, str, int, str)
    delete_clip = Signal(str)
    rename_bookmark = Signal(str)
    delete_bookmark = Signal(str)
    rename_track = Signal(str)
    duplicate_track = Signal(str)
    delete_track = Signal(str)
    reorder_track = Signal(str, int)

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.viewport().setMouseTracking(True)
        self.horizontalScrollBar().setSingleStep(64)
        self.verticalScrollBar().setSingleStep(LANE_HEIGHT)
        self.horizontalScrollBar().valueChanged.connect(self.viewport().update)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)
        self.project: Project | None = None
        self.timeline: Timeline | None = None
        self.rows: list[TrackRow] = []
        self.text_positions: list[int] = []
        self.text_labels: list[str] = []
        self.playhead = 0
        self.active = ""
        self.selected_track = ""
        self.hovered_track = ""
        self.scale = 1.6
        self.snap = True
        self.drag = None
        self.ghost = None
        self.track_drag = None
        self.track_drop_index: int | None = None
        self.track_drag_y = 0.0
        self.bookmark_rects: list[tuple[QRectF, str, int]] = []

    def bind(self, project: Project, timeline: Timeline) -> None:
        self.project, self.timeline = project, timeline
        text = {cue.id: cue.text.replace("\n", " ") for cue in project.cues}
        self.text_positions = list(timeline.dialogue_starts.values())
        self.text_labels = [text[cue_id] for cue_id in timeline.dialogue_starts]
        choices = {track.id: [] for track in project.media.tracks}
        if self.selected_track not in choices:
            self.selected_track = ""
        if self.hovered_track not in choices:
            self.hovered_track = ""
        for clip in timeline.media.clips.values():
            choices[clip.track].append(clip)
        self.rows.clear()
        top = 0
        for track in project.media.tracks:
            endings = []
            placements = []
            for clip in sorted(choices[track.id], key=lambda item: item.start):
                lane = next((i for i, end in enumerate(endings) if end <= clip.start), len(endings))
                if lane == len(endings):
                    endings.append(clip.end)
                else:
                    endings[lane] = clip.end
                placements.append((clip, lane))
            height = max(1, len(endings)) * LANE_HEIGHT + 4
            self.rows.append(TrackRow(track, top, height, placements))
            top += height
        self._voice_labels: dict[str, str] = {}
        self._voice_starts: list[int] = []
        if timeline.voices:
            voice_track = MediaTrack(VOICE_TRACK_ID, "角色配音", "voice")
            placements = []
            for span in timeline.voices:
                vclip = MediaClip(span.id, VOICE_TRACK_ID, span.asset, span.start, span.length, volume=span.volume)
                self._voice_labels[span.id] = span.label
                self._voice_starts.append(span.start)
                placements.append((vclip, 0))
            height = LANE_HEIGHT + 4
            self.rows.append(TrackRow(voice_track, top, height, placements))
            top += height
        self._scroll_ranges()
        self.viewport().update()

    def reset(self) -> None:
        self.active = ""
        self.selected_track = ""
        self.hovered_track = ""
        self.scale = 1.6
        self.playhead = 0
        self.drag = self.ghost = None
        self.track_drag = None
        self.track_drop_index = None
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(0)

    def select(self, clip_id: str) -> None:
        self.active = clip_id
        if clip_id:
            self.selected_track = ""
        self.viewport().update()

    def select_track(self, track_id: str) -> None:
        self.selected_track = track_id
        if track_id:
            self.active = ""
        self.viewport().update()

    def set_position(self, frame: int, reveal: bool = False) -> None:
        self.playhead = frame
        if reveal and not LABEL_WIDTH <= self._x(frame) < self.viewport().width():
            self.horizontalScrollBar().setValue(self._scroll_value(frame * self.scale - self._lane_width() / 2))
        self.viewport().update()

    def _lane_width(self) -> int:
        return max(1, self.viewport().width() - LABEL_WIDTH)

    def _scroll_value(self, pixels: float) -> int:
        return max(0, min(2**31 - 1, round(pixels)))

    def _scroll_ranges(self) -> None:
        length = self.timeline.length if self.timeline else 0
        if self.project:
            length = max(length, *(clip.end for clip in self.timeline.media.clips.values()),
                         *(bookmark.frame for bookmark in self.project.media.bookmarks), 0)
        track_size = sum(row.height for row in self.rows)
        left = self.horizontalScrollBar()
        left.setPageStep(self._lane_width())
        left.setRange(0, self._scroll_value(length * self.scale + max(15 * FPS * self.scale, self._lane_width() / 2)
                                            - self._lane_width()))
        vertical = self.verticalScrollBar()
        vertical.setPageStep(max(1, self.viewport().height() - TRACK_TOP))
        vertical.setRange(0, max(0, track_size - vertical.pageStep()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scroll_ranges()

    def zoom(self, factor: float, point: float | None = None) -> None:
        point = point if point is not None else self._x(self.playhead)
        if not LABEL_WIDTH <= point <= self.viewport().width():
            point = LABEL_WIDTH + self._lane_width() / 2
        target = self._frame(point)
        self.scale = max(0.00002, min(12, self.scale * factor))
        self._scroll_ranges()
        self.horizontalScrollBar().setValue(self._scroll_value(target * self.scale - (point - LABEL_WIDTH)))
        self.viewport().update()

    def fit(self) -> None:
        length = self.timeline.length if self.timeline else 0
        self.scale = max(0.00002, min(12, (self._lane_width() - 8) / max(length, 2 * FPS)))
        self._scroll_ranges()
        self.horizontalScrollBar().setValue(0)
        self.viewport().update()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.35 ** (event.angleDelta().y() / 120), event.position().x())
            event.accept()
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            cursor = self.horizontalScrollBar()
            cursor.setValue(cursor.value() - event.angleDelta().y())
            event.accept()
        else:
            super().wheelEvent(event)

    def _x(self, frame: int) -> float:
        return LABEL_WIDTH + frame * self.scale - self.horizontalScrollBar().value()

    def _frame(self, x: float) -> int:
        return round((x - LABEL_WIDTH + self.horizontalScrollBar().value()) / self.scale)

    def _adjusted_frame(self, frame: int) -> int:
        frame = max(0, min(MAX_MEDIA_FRAME - 1, frame))
        if self.snap and self.timeline:
            i = bisect_left(self.text_positions, frame)
            edges = [self.playhead, self.timeline.dialogue_end, *self.text_positions[max(0, i - 1):i + 1]]
            nearest = min(edges, key=lambda value: abs(value - frame))
            if abs(frame - nearest) * self.scale <= 8:
                return min(MAX_MEDIA_FRAME - 1, nearest)
        return frame

    def _track_y(self, row: TrackRow) -> int:
        return TRACK_TOP + row.top - self.verticalScrollBar().value()

    def _row_at(self, y: float) -> TrackRow | None:
        if y >= TRACK_TOP:
            return next((row for row in self.rows if self._track_y(row) <= y < self._track_y(row) + row.height), None)
        return None

    def _clip_rect(self, row: TrackRow, clip: MediaClip, lane: int, start: int | None = None,
                   end: int | None = None) -> QRectF:
        start = clip.start if start is None else start
        end = clip.end if end is None else end
        return QRectF(self._x(start), self._track_y(row) + 4 + lane * LANE_HEIGHT,
                      max(2, (end - start) * self.scale), LANE_HEIGHT - 4)

    def _clip_at(self, point):
        row = self._row_at(point.y())
        if row and point.x() >= LABEL_WIDTH:
            for clip, lane in row.clips:
                rect = self._clip_rect(row, clip, lane)
                if rect.contains(point):
                    return clip, rect
        return None, None

    def _clip_label(self, clip: MediaClip) -> str:
        if clip.track == VOICE_TRACK_ID:
            return self._voice_labels.get(clip.id, "角色配音")
        name = self.project.assets[clip.asset].name
        if clip.id in self.timeline.media.pending:
            return "待定位  " + name
        if clip.id in self.timeline.media.conflicting_clips:
            return "背景重叠  " + name
        return name

    def _label(self, painter: QPainter, box: QRectF, string: str, color: QColor | None = None) -> None:
        if box.width() < 7:
            return
        painter.setPen(self.palette().text().color() if color is None else color)
        content = painter.fontMetrics().elidedText(string, Qt.TextElideMode.ElideRight, int(box.width()))
        painter.drawText(box, Qt.AlignmentFlag.AlignVCenter, content)

    def _paint_tracks(self, painter: QPainter, width: int, height: int) -> None:
        palette = self.palette()
        painter.save()
        painter.setClipRect(QRectF(0, TRACK_TOP, width, max(0, height - TRACK_TOP)))
        dragging_id = self.track_drag[0] if self.track_drag and self.track_drop_index is not None else ""
        for row in self.rows:
            top = self._track_y(row)
            if top + row.height <= TRACK_TOP or top >= height:
                continue
            selected_row = row.track.id == self.selected_track
            hovered_row = row.track.id == self.hovered_track
            head_brush = (palette.highlight() if selected_row else
                          palette.light() if hovered_row else palette.midlight())
            painter.fillRect(QRectF(0, top, LABEL_WIDTH, row.height), head_brush)
            if selected_row:
                painter.fillRect(QRectF(0, top, 3, row.height), palette.accent())
            painter.fillRect(QRectF(LABEL_WIDTH, top, width - LABEL_WIDTH, row.height), palette.alternateBase())
            painter.setPen(palette.mid().color())
            painter.drawLine(0, top + row.height - 1, width, top + row.height - 1)
            painter.drawLine(LABEL_WIDTH - 1, top, LABEL_WIDTH - 1, top + row.height - 1)
            self._label(painter, QRectF(11, top, LABEL_WIDTH - 30, row.height), row.track.name,
                        palette.accent().color() if row.track.id == VOICE_TRACK_ID else None)
            if row.track.id != VOICE_TRACK_ID:
                grip_color = palette.accent().color() if (selected_row or hovered_row) else palette.shadow().color()
                mid_y = top + row.height / 2
                for dx in (-15, -10):
                    for dy in (-5, -1, 3):
                        painter.fillRect(QRectF(LABEL_WIDTH + dx, mid_y + dy, 2, 2), grip_color)
            painter.save()
            painter.setClipRect(QRectF(LABEL_WIDTH, TRACK_TOP, width - LABEL_WIDTH, height - TRACK_TOP))
            if row.track.id == VOICE_TRACK_ID and self._voice_starts:
                left_frame = max(0, self._frame(LABEL_WIDTH))
                right_frame = self._frame(width)
                first_v = max(0, bisect_right(self._voice_starts, left_frame) - 1)
                last_v = bisect_right(self._voice_starts, right_frame)
                visible_clips = row.clips[first_v:last_v]
            else:
                visible_clips = row.clips
            for clip, lane in visible_clips:
                rect = self._clip_rect(row, clip, lane)
                if rect.right() < LABEL_WIDTH or rect.left() > width:
                    continue
                selected = clip.id == self.active
                outline = (QColor("#de9d7e") if clip.id in self.timeline.media.conflicting_clips else
                           QColor("#dec494") if clip.id in self.timeline.media.pending else
                           palette.accent().color() if selected else palette.shadow().color())
                painter.setPen(QPen(outline, 2 if selected else 1))
                painter.setBrush(QColor(TRACK_COLORS[row.track.kind]))
                painter.drawRoundedRect(rect, 4, 4)
                if rect.width() > 17:
                    self._label(painter, rect.adjusted(6, 0, -5, 0), self._clip_label(clip))
            painter.restore()
            if row.track.id == dragging_id:
                veil = QColor(palette.dark().color())
                veil.setAlpha(135)
                painter.fillRect(QRectF(0, top, width, row.height), veil)
        if self.ghost:
            track, start, end = self.ghost
            row = next((row for row in self.rows if row.track.id == track), None)
            if row:
                painter.setClipRect(QRectF(LABEL_WIDTH, TRACK_TOP, width - LABEL_WIDTH, height - TRACK_TOP))
                accent = palette.accent().color()
                painter.setPen(QPen(accent, 2, Qt.PenStyle.DashLine))
                accent.setAlpha(45)
                painter.setBrush(accent)
                painter.drawRoundedRect(QRectF(self._x(start), self._track_y(row) + 4,
                                               max(2, (end - start) * self.scale), LANE_HEIGHT - 4), 4, 4)
        if dragging_id and self.rows:
            target = max(0, min(len(self.rows), self.track_drop_index))
            if target < len(self.rows):
                line_y = float(self._track_y(self.rows[target]))
            else:
                last = self.rows[-1]
                line_y = float(self._track_y(last) + last.height)
            line_y = max(float(TRACK_TOP + 1), min(float(height - 2), line_y))
            accent = palette.accent().color()
            painter.setPen(QPen(accent, 2))
            painter.drawLine(QPointF(0, line_y), QPointF(width, line_y))
            painter.setBrush(accent)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(6, line_y), 3.5, 3.5)
            source = next((item for item in self.rows if item.track.id == dragging_id), None)
            if source:
                ghost_y = max(float(TRACK_TOP + 2), min(float(height - 30), self.track_drag_y - 14))
                ghost_rect = QRectF(8, ghost_y, min(220, width - 16), 28)
                fill = QColor(palette.highlight().color())
                fill.setAlpha(225)
                painter.setPen(QPen(accent, 1.5))
                painter.setBrush(fill)
                painter.drawRoundedRect(ghost_rect, 5, 5)
                self._label(painter, ghost_rect.adjusted(10, 0, -10, 0), source.track.name)
        painter.restore()

    def _dialogue_rect(self, index: int) -> QRectF:
        begin = self.text_positions[index]
        end = self.text_positions[index + 1] if index + 1 < len(self.text_positions) else self.timeline.dialogue_end
        x = self._x(begin)
        width = self._x(end) - x
        inset = min(3, width / 4)
        return QRectF(x + inset, RULER_HEIGHT + BOOKMARK_HEIGHT + 3, width - 2 * inset, DIALOGUE_HEIGHT - 6)

    def _dialogue_at(self, point: QPointF) -> int | None:
        if point.x() < LABEL_WIDTH or not RULER_HEIGHT + BOOKMARK_HEIGHT <= point.y() < TRACK_TOP:
            return None
        frame = (point.x() - self._x(0)) / self.scale
        index = bisect_right(self.text_positions, frame) - 1
        return index if index >= 0 and self._dialogue_rect(index).contains(point) else None

    def _paint_dialogue(self, painter: QPainter, left: int, right: int, width: int) -> None:
        palette = self.palette()
        first = max(0, bisect_right(self.text_positions, left) - 1)
        last = bisect_right(self.text_positions, right)
        active = -1
        if self.timeline and self.playhead < self.timeline.dialogue_end:
            active = bisect_right(self.text_positions, self.playhead) - 1
        lane = QRectF(LABEL_WIDTH, RULER_HEIGHT + BOOKMARK_HEIGHT, width - LABEL_WIDTH, DIALOGUE_HEIGHT)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for index in range(first, last):
            rect = self._dialogue_rect(index)
            fill = palette.highlight() if index == active else palette.light()
            if rect.width() < 8:
                painter.fillRect(rect, fill)
                continue
            painter.setPen(palette.accent().color() if index == active else palette.shadow().color())
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 4, 4)
            visible = rect.intersected(lane)
            if visible.width() > 32:
                self._label(painter, visible.adjusted(5, 0, -4, 0), self.text_labels[index])
        painter.restore()

    def _paint_ruler(self, painter: QPainter, width: int, height: int) -> None:
        palette = self.palette()
        painter.fillRect(QRectF(0, 0, width, TRACK_TOP), palette.midlight())
        painter.setPen(palette.mid().color())
        painter.drawLine(0, TRACK_TOP - 1, width, TRACK_TOP - 1)
        self._label(painter, QRectF(11, 0, LABEL_WIDTH - 18, RULER_HEIGHT), "成片时间")
        self._label(painter, QRectF(11, RULER_HEIGHT, LABEL_WIDTH - 18, BOOKMARK_HEIGHT), "书签")
        self._label(painter, QRectF(11, RULER_HEIGHT + BOOKMARK_HEIGHT, LABEL_WIDTH - 18, DIALOGUE_HEIGHT), "台词定位")
        left, right = max(0, self._frame(LABEL_WIDTH)), self._frame(width)
        painter.save()
        painter.setClipRect(QRectF(LABEL_WIDTH, 0, width - LABEL_WIDTH, height))
        ticks = (1, 3, 6, 15, 30, 60, 150, 300, 600, 1800, 3600, 9000, 18000, 54000, 108000)
        step = next((step for step in ticks if step * self.scale >= 84), max(108000, round(84 / self.scale)))
        for frame in range(left // step * step, right + step, step):
            x = self._x(frame)
            painter.setPen(palette.mid().color())
            painter.drawLine(QPointF(x, 22), QPointF(x, height))
            self._label(painter, QRectF(x + 4, 1, 99, RULER_HEIGHT - 3), timecode(frame, step < FPS),
                        palette.placeholderText().color())

        self._paint_dialogue(painter, left, right, width)
        self.bookmark_rects.clear()
        next_x = float(LABEL_WIDTH)
        if self.project:
            for bookmark in sorted(self.project.media.bookmarks, key=lambda mark: mark.frame):
                x = self._x(bookmark.frame)
                if not LABEL_WIDTH <= x <= width:
                    continue
                painter.setPen(palette.accent().color())
                painter.drawLine(QPointF(x, RULER_HEIGHT), QPointF(x, RULER_HEIGHT + BOOKMARK_HEIGHT))
                label_x = max(x + 1, next_x)
                size = min(122, painter.fontMetrics().horizontalAdvance(bookmark.name) + 16)
                rect = QRectF(label_x, RULER_HEIGHT + 2, size, BOOKMARK_HEIGHT - 4)
                painter.fillRect(rect, palette.highlight())
                self._label(painter, rect.adjusted(6, 0, -4, 0), bookmark.name)
                self.bookmark_rects.append((rect, bookmark.id, bookmark.frame))
                next_x = rect.right() + 2
        x = self._x(self.playhead)
        painter.setPen(QPen(palette.accent().color(), 1.5))
        painter.drawLine(QPointF(x, 0), QPointF(x, height))
        painter.setBrush(palette.accent())
        painter.drawPolygon(QPolygonF([QPointF(x - 4, 0), QPointF(x + 4, 0),
                                       QPointF(x + 4, 10), QPointF(x, 14), QPointF(x - 4, 10)]))
        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        try:
            width, height = self.viewport().width(), self.viewport().height()
            painter.fillRect(self.viewport().rect(), self.palette().dark())
            self._paint_tracks(painter, width, height)
            self._paint_ruler(painter, width, height)
        finally:
            painter.end()

    def _bookmark_at(self, point):
        return next(((mark, time) for rect, mark, time in self.bookmark_rects if rect.contains(point)), None)

    def _drag_mode(self, point: QPointF, rect: QRectF) -> str:
        if rect.width() >= 18:
            if point.x() - rect.left() <= 6:
                return "left"
            if rect.right() - point.x() <= 6:
                return "right"
        return "move"

    def _track_drop_target(self, y: float) -> int:
        editable = [row for row in self.rows if row.track.id != VOICE_TRACK_ID]
        for index, row in enumerate(editable):
            if y < self._track_y(row) + row.height / 2:
                return index
        return len(editable)

    def mousePressEvent(self, event):
        if not self.project:
            return
        point = event.position()
        mark = self._bookmark_at(point)
        clip, rect = self._clip_at(point)
        track_row = self._row_at(point.y()) if point.x() < LABEL_WIDTH else None
        if event.button() == Qt.MouseButton.RightButton:
            self._context_menu(point, clip, mark, track_row)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if track_row:
            if track_row.track.id == VOICE_TRACK_ID:
                self.selection.emit("")
                self.select_track("")
                return
            self.selection.emit("")
            self.select_track(track_row.track.id)
            index = next(i for i, item in enumerate(self.rows) if item.track.id == track_row.track.id)
            self.track_drag = (track_row.track.id, index, point.y())
            self.track_drop_index = None
            self.track_drag_y = point.y()
            return
        if mark:
            self.seek.emit(mark[1])
        elif clip:
            if clip.track == VOICE_TRACK_ID:
                self.selected_track = ""
                self.selection.emit("")
                self.seek.emit(clip.start)
                return
            self.selected_track = ""
            self.selection.emit(clip.id)
            self.drag = (clip, self._drag_mode(point, rect), point.x(), point.y(), self._frame(point.x()))
        elif (dialogue := self._dialogue_at(point)) is not None:
            self.seek.emit(self.text_positions[dialogue])
        elif point.x() >= LABEL_WIDTH:
            self.selected_track = ""
            self.selection.emit("")
            self.seek.emit(max(0, self._frame(point.x())))

    def _move_at(self, point):
        clip, mode, _, _, origin = self.drag
        row = self._row_at(point.y()) if mode == "move" else None
        if row:
            if row.track.id == VOICE_TRACK_ID:
                row = None
            else:
                original_kind = next(row.track.kind for row in self.rows if row.track.id == clip.track)
                if (row.track.kind == "background") != (original_kind == "background"):
                    row = None
        track = row.track.id if row else clip.track
        delta = self._frame(point.x()) - origin
        if mode == "right":
            return track, clip.start, max(clip.start + 1, self._adjusted_frame(clip.end + delta))
        start = self._adjusted_frame(clip.start + delta)
        if mode == "left":
            return track, min(clip.end - 1, start), clip.end
        return track, start, start + clip.length

    def mouseMoveEvent(self, event):
        point = event.position()
        if self.track_drag:
            _, _, origin_y = self.track_drag
            if self.track_drop_index is not None or abs(point.y() - origin_y) >= 4:
                self.track_drop_index = self._track_drop_target(point.y())
                self.track_drag_y = point.y()
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                self.viewport().update()
            return
        if not self.drag:
            track_row = self._row_at(point.y()) if point.x() < LABEL_WIDTH else None
            hovered = track_row.track.id if track_row else ""
            if hovered != self.hovered_track:
                self.hovered_track = hovered
                self.viewport().update()
            clip, rect = self._clip_at(point)
            if track_row:
                self.viewport().setToolTip(f"{track_row.track.name}　拖动调整上下顺序，右键打开菜单")
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            elif clip:
                self.viewport().setToolTip(self._clip_label(clip))
                cursor = Qt.CursorShape.SizeHorCursor if self._drag_mode(point, rect) != "move" else Qt.CursorShape.OpenHandCursor
                self.viewport().setCursor(cursor)
            elif (mark := self._bookmark_at(point)) is not None:
                bookmark = next((item for item in self.project.media.bookmarks if item.id == mark[0]), None)
                self.viewport().setToolTip(f"{bookmark.name}　{timecode(mark[1], True)}" if bookmark else "")
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            elif (dialogue := self._dialogue_at(point)) is not None:
                self.viewport().setToolTip(self.text_labels[dialogue])
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.viewport().setToolTip("")
                self.viewport().unsetCursor()
            return
        _, _, x, y, _ = self.drag
        if self.ghost or abs(point.x() - x) + abs(point.y() - y) >= 4:
            self.ghost = self._move_at(point)
            self.viewport().update()

    def leaveEvent(self, event):
        if self.hovered_track:
            self.hovered_track = ""
            self.viewport().update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.track_drag and event.button() == Qt.MouseButton.LeftButton:
            track_id, src_index, _ = self.track_drag
            drop_index = self.track_drop_index
            self.track_drag = None
            self.track_drop_index = None
            self.viewport().unsetCursor()
            if drop_index is not None:
                final_index = drop_index - 1 if drop_index > src_index else drop_index
                if final_index != src_index:
                    self.reorder_track.emit(track_id, final_index)
            self.viewport().update()
            return
        if self.drag and event.button() == Qt.MouseButton.LeftButton:
            clip, mode, _, _, _ = self.drag
            ghost = self.ghost
            self.drag = self.ghost = None
            if ghost:
                track, start, end = ghost
                self.adjust_clip.emit(clip.id, mode, end if mode == "right" else start, track)
            self.viewport().update()

    def mouseDoubleClickEvent(self, event):
        point = event.position()
        if mark := self._bookmark_at(point):
            self.rename_bookmark.emit(mark[0])
        elif (clip := self._clip_at(point)[0]) is not None:
            self.seek.emit(clip.start)
        elif point.x() < LABEL_WIDTH and (row := self._row_at(point.y())):
            self.rename_track.emit(row.track.id)

    def _context_menu(self, point, clip, mark, track_row: TrackRow | None = None) -> None:
        if not clip and not mark and not track_row:
            return
        menu = QMenu(self)
        if track_row:
            if track_row.track.id == VOICE_TRACK_ID:
                return
            self.selection.emit("")
            self.select_track(track_row.track.id)
            index = next(i for i, item in enumerate(self.rows) if item.track.id == track_row.track.id)
            is_bg = track_row.track.kind == "background"
            menu.addAction("轨道改名\tF2", lambda: self.rename_track.emit(track_row.track.id))
            dup = menu.addAction("复制轨道", lambda: self.duplicate_track.emit(track_row.track.id))
            dup.setEnabled(not is_bg)
            menu.addSeparator()
            up = menu.addAction("上移", lambda: self.reorder_track.emit(track_row.track.id, index - 1))
            up.setEnabled(index > 0)
            down = menu.addAction("下移", lambda: self.reorder_track.emit(track_row.track.id, index + 1))
            down.setEnabled(index < len(self.rows) - 1)
            menu.addSeparator()
            remove = menu.addAction("删除轨道\tDelete", lambda: self.delete_track.emit(track_row.track.id))
            remove.setEnabled(not is_bg)
        elif clip:
            if clip.track == VOICE_TRACK_ID:
                menu.addAction("跳到配音起点", lambda: self.seek.emit(clip.start))
                menu.aboutToHide.connect(menu.deleteLater)
                menu.popup(self.viewport().mapToGlobal(point.toPoint()))
                return
            self.selection.emit(clip.id)
            menu.addAction("跳到片段起点", lambda: self.seek.emit(clip.start))
            menu.addAction("删除片段\tDelete", lambda: self.delete_clip.emit(clip.id))
        else:
            menu.addAction("书签改名", lambda: self.rename_bookmark.emit(mark[0]))
            menu.addAction("删除书签", lambda: self.delete_bookmark.emit(mark[0]))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.viewport().mapToGlobal(point.toPoint()))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            if self.active:
                self.delete_clip.emit(self.active)
                return
            if self.selected_track:
                row = next((item for item in self.rows if item.track.id == self.selected_track), None)
                if row and row.track.kind != "background":
                    self.delete_track.emit(self.selected_track)
                return
        elif event.key() == Qt.Key.Key_F2 and self.selected_track:
            self.rename_track.emit(self.selected_track)
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if self.project and event.mimeData().hasFormat(ASSET_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(ASSET_MIME) and self._row_at(event.position().y()):
            if event.position().x() >= LABEL_WIDTH:
                event.acceptProposedAction()

    def dropEvent(self, event):
        point = event.position()
        if (event.mimeData().hasFormat(ASSET_MIME) and point.x() >= LABEL_WIDTH
                and (row := self._row_at(point.y())) and row.track.id != VOICE_TRACK_ID):
            ref = bytes(event.mimeData().data(ASSET_MIME)).decode("ascii", errors="ignore")
            self.drop_asset.emit(ref, row.track.id, self._adjusted_frame(self._frame(point.x())))
            event.acceptProposedAction()
