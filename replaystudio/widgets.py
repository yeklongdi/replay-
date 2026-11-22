from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from string import Template

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QObject, QPointF, QRectF, QSortFilterProxyModel,
    Qt, Signal,
)
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QFrame, QPushButton, QWidget

from .model import FPS, HEIGHT, WIDTH, Box, Project, TEMPLATE_MINIMUMS
from .render import Images, Page, as_rect, draw_page, font

MARKERS = {"scene": "场景", "interlude": "过场", "battle_start": "战斗开始", "battle_end": "战斗结束"}

STYLESHEET = Template("""
QWidget { color: $text; font-size: 13px; }
QMainWindow, QDialog, QMessageBox { background: $background; }
QFrame[panel="true"], QWidget[panel="true"] { background: $panel; border: 1px solid $line; border-radius: 9px; }
QFrame#workspaceBar { background: $background; border-bottom: 1px solid $line; }
QWidget#inspectorBody { background: $panel; }
QWidget#emptyStage { background: $well; border-radius: 6px; }
QLabel#panelTitle { color: $text; font-size: 15px; font-weight: 650; }
QLabel#muted, QLabel#meta { color: $muted; }
QLabel#meta, QLabel#fieldTitle, QLabel#status { font-size: 12px; }
QLabel#sectionLabel { color: $text; font-weight: 600; }
QLabel#time { color: $accent; font-family: Consolas; font-size: 13px; font-weight: 600; }
QLabel#fileLabel { color: $muted; padding: 6px 7px; background: $field; border: 1px solid $line; border-radius: 5px; }
QLabel#rangeLabel { color: $muted; font-size: 12px; padding: 2px 7px; background: $well; border: 1px solid $line; border-radius: 4px; }
QLabel#rangeLabel[marked="true"] { color: $accent; border-color: $grip; }
QLabel#noteWell { color: $muted; font-size: 12px; padding: 6px 8px; background: $well; border: 1px solid $line; border-radius: 6px; }
QFrame#toolbarDivider { background: $line; max-width: 1px; min-width: 1px; margin: 3px 2px; }
QLabel#muted:disabled, QLabel:disabled { color: $dim; }
QPushButton {
    padding: 6px 11px; min-height: 19px; border: 1px solid $line;
    border-radius: 6px; background: $control; font-weight: 550;
}
QPushButton:hover { background: $hover; border-color: $grip; }
QPushButton:pressed { background: $selection; }
QPushButton:focus { border-color: $accent; }
QPushButton:disabled { color: $dim; background: $panel; border-color: $line; }
QPushButton[variant="quiet"] { background: $panel; }
QPushButton[variant="quiet"]:hover { background: $hover; }
QPushButton[variant="settings"] {
    padding: 5px 22px 5px 9px; min-height: 17px; color: $muted;
    background: transparent; border-color: transparent;
}
QPushButton[variant="settings"]:hover, QPushButton[variant="settings"]:open {
    color: $text; background: $control; border-color: $line;
}
QPushButton[variant="settings"]:focus { border-color: $accent; }
QPushButton::menu-indicator {
    subcontrol-origin: border; subcontrol-position: right center;
    right: 6px; width: 10px; height: 10px;
}
QPushButton[variant="accent"], QPushButton[variant="play"] { background: $accent; border-color: $accent; color: $accent_text; font-weight: 700; }
QPushButton[variant="accent"]:disabled, QPushButton[variant="play"]:disabled { background: $control; border-color: $line; color: $dim; }
QPushButton[variant="compact"] { padding: 4px 7px; min-height: 17px; }
QPushButton[variant="compact"][hasMenu="true"] { padding-right: 22px; }
QPushButton[variant="mode"] { padding: 5px 6px; min-height: 17px; color: $muted; background: $control; }
QPushButton[variant="mode"]:hover { color: $text; background: $hover; }
QPushButton[variant="mode"]:checked { color: $text; background: $selection; border-color: $accent; font-weight: 700; }
QPushButton[variant="play"] { min-width: 45px; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: $field; border: 1px solid $line; border-radius: 6px; padding: 5px 8px;
    selection-background-color: $selection; selection-color: $text; min-height: 19px;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: $accent; }
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled { color: $dim; border-color: $line; }
QPlainTextEdit { padding: 8px; }
QLineEdit { placeholder-text-color: $muted; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 18px; border-left: 1px solid $line; background: $selection; }
QComboBox::down-arrow { image: url(:/qt-project.org/styles/commonstyle/images/arrow-down-16.png); width: 10px; height: 10px; }
QComboBox QAbstractItemView { background: $control; color: $text; border: 1px solid $line; selection-background-color: $selection; outline: 0; }
QMenu { background: $header; border: 1px solid $line; padding: 4px; }
QMenu::item { padding: 5px 20px 5px 10px; border-radius: 3px; }
QMenu::item:selected { background: $selection; }
QMenu::item:disabled { color: $dim; }
QMenu::separator { height: 1px; background: $line; margin: 3px 5px; }
QTableView, QListWidget {
    background: $list_base; alternate-background-color: $list_alt; outline: 0; border: 1px solid $line; border-radius: 7px;
    selection-background-color: $selection; selection-color: $text; color: $text; gridline-color: $line;
}
QTableView::item, QListWidget::item { padding: 5px; border: 0; }
QTableView::item:hover, QListWidget::item:hover { background: $hover; }
QTableView::item:selected:hover, QListWidget::item:selected:hover { background: $selection; }
QHeaderView { background: $header; }
QHeaderView::section { color: $muted; font-size: 12px; font-weight: 600; background: $header; padding: 7px; border: 0; border-bottom: 1px solid $line; }
QTabWidget::pane { border: 0; border-top: 1px solid $line; }
QTabBar::tab { padding: 9px 9px; margin-right: 2px; color: $muted; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: $text; border-bottom: 2px solid $accent; font-weight: 700; }
QTabBar::tab:hover { color: $text; background: $header; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { width: 10px; background: $list_base; border: 0; }
QScrollBar:horizontal { height: 10px; background: $list_base; border: 0; }
QScrollBar::handle { background: $grip; border: 2px solid $list_base; border-radius: 5px; min-width: 24px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { border: none; height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSplitter::handle { background: $background; width: 8px; height: 8px; }
QSplitter::handle:hover { background: $grip; }
QSlider::groove:horizontal { height: 5px; border-radius: 3px; background: $line; }
QSlider::sub-page:horizontal { background: $grip; border-radius: 3px; }
QSlider::handle:horizontal { width: 12px; margin-top: -5px; margin-bottom: -5px; border-radius: 6px; background: $accent; }
QProgressBar { background: $well; border: 1px solid $line; border-radius: 6px; min-height: 17px; text-align: center; color: $text; }
QProgressBar::chunk { background: $grip; border-radius: 5px; }
QToolTip { color: $text; background: $control; border: 1px solid $line; padding: 5px; }
QStatusBar { color: $muted; background: $background; font-size: 11px; padding: 0; }
QStatusBar::item { border: 0; }
""")


def button(label: str, variant: str = "", parent: QWidget | None = None) -> QPushButton:
    item = QPushButton(label, parent)
    if variant:
        item.setProperty("variant", variant)
    item.setCursor(Qt.CursorShape.PointingHandCursor)
    return item


def panel() -> QFrame:
    box = QFrame()
    box.setProperty("panel", True)
    return box


def app_icon() -> QIcon:
    image = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#bce3c8"))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    painter.setBrush(QColor("#24453b"))
    painter.drawPolygon(QPolygonF([QPointF(24, 18), QPointF(48, 32), QPointF(24, 46)]))
    painter.end()
    return QIcon(QPixmap.fromImage(image))


def timecode(frame: int, precise: bool = False) -> str:
    seconds, remainder = divmod(frame, FPS)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    suffix = f".{remainder * 1000 // FPS:03}" if precise else ""
    return f"{hours:02}:{minutes:02}:{seconds:02}{suffix}"


class CueModel(QAbstractTableModel):
    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.project: Project | None = None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self.project.cues) if self.project and not parent.isValid() else 0

    def columnCount(self, parent=QModelIndex()) -> int:
        return 3

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ("时间", "角色", "内容")[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.project is None:
            return None
        cue = self.project.cues[index.row()]
        column = index.column()
        marker = MARKERS.get(cue.kind, "")
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return cue.stamp.rsplit(" ", 1)[-1] or "—"
            if column == 1:
                actor = self.project.roles.get(cue.role)
                return marker or (actor.name if actor else "旁白")
            if cue.kind == "scene":
                return self.project.scenes[cue.scene].name
            if not cue.is_text:
                return "范围标记"
            text = " ".join(cue.text.split())
            return text[:150] + "…" if len(text) > 150 else text or "空记录"
        if role == Qt.ItemDataRole.ToolTipRole:
            if cue.stamp:
                return cue.stamp
            return marker or "补写台词"
        if role == Qt.ItemDataRole.ForegroundRole:
            palette = QApplication.palette()
            if cue.hidden or (cue.is_text and not cue.text.strip()):
                return palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
            if marker or cue.initiative:
                return palette.accent().color()
        return None

    def bind(self, project: Project | None) -> None:
        self.beginResetModel()
        self.project = project
        self.endResetModel()

    def update_row(self, row: int) -> None:
        self.dataChanged.emit(self.index(row, 0), self.index(row, 2))

    def update_all(self) -> None:
        if self.rowCount():
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, 2))


class CueFilter(QSortFilterProxyModel):
    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.setDynamicSortFilter(False)
        self.criteria = ("", "", "", "")

    def apply(self, keyword: str, role: str, period: str | int, visibility: str) -> None:
        self.beginFilterChange()
        self.criteria = (keyword.strip().casefold(), role, period, visibility)
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row, source_parent):
        project = self.sourceModel().project
        if project is None:
            return False
        word, role, period, visibility = self.criteria
        cue = project.cues[source_row]
        empty = cue.is_text and not cue.text.strip()
        if role and cue.role != role:
            return False
        if isinstance(period, int):
            if not any(project.messages[number - 1].session == period for number in cue.sources):
                return False
        elif period and not cue.stamp.startswith(period):
            return False
        if visibility == "hidden" and not cue.hidden:
            return False
        if visibility == "shown" and (cue.hidden or empty):
            return False
        if visibility == "empty" and not empty:
            return False
        if not word:
            return True
        actor = project.roles.get(cue.role)
        scene = project.scenes.get(cue.scene)
        return any(word in value.casefold() for value in (
            cue.text, cue.stamp, actor.name if actor else "", MARKERS.get(cue.kind, ""),
            scene.name if scene else "",
        ))


class Stage(QWidget):
    template_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(90)
        self.project: Project | None = None
        self.images: Images | None = None
        self.page: Page | None = None
        self.background = ""
        self.adjusting = False
        self.drag = None
        self.active: str = ""

    def bind(self, project: Project, media: Path) -> None:
        self.project, self.images = project, Images(media)
        self.page = None
        self.background = ""
        self.drag = None
        self.active = ""
        self.update()

    def display(self, page: Page | None, background: str) -> None:
        if self.page is not page or self.background != background:
            self.page, self.background = page, background
            self.update()

    def frame_rect(self) -> QRectF:
        size = self.rect().adjusted(8, 8, -8, -8)
        scale = min(size.width() / WIDTH, size.height() / HEIGHT)
        return QRectF(size.center().x() - WIDTH * scale / 2, size.center().y() - HEIGHT * scale / 2,
                      WIDTH * scale, HEIGHT * scale)

    def _editing_guides(self, painter: QPainter) -> None:
        labels = {"portrait": "立绘", "speaker": "姓名", "dialogue": "对话"}
        for key in TEMPLATE_MINIMUMS:
            rect = as_rect(getattr(self.project.template, key))
            role = QPalette.ColorRole.Accent if key == self.active else QPalette.ColorRole.PlaceholderText
            color = self.palette().color(role)
            painter.setPen(QPen(color, 3, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRect(QRectF(rect.right() - 22, rect.bottom() - 22, 22, 22))
            painter.setPen(color)
            painter.setFont(font(25, True))
            painter.drawText(QPointF(rect.left() + 9, rect.top() + 31), labels[key])

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), self.palette().dark())
            rect = self.frame_rect()
            if not self.project or rect.width() <= 0:
                return
            painter.save()
            painter.setClipRect(rect)
            painter.translate(rect.topLeft())
            painter.scale(rect.width() / WIDTH, rect.height() / HEIGHT)
            draw_page(painter, self.project, self.page, self.images, self.background)
            if self.adjusting:
                self._editing_guides(painter)
            painter.restore()
            painter.setPen(self.palette().mid().color())
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
        finally:
            painter.end()

    def _to_canvas(self, position: QPointF) -> QPointF:
        rect = self.frame_rect()
        return (position - rect.topLeft()) * (WIDTH / max(1, rect.width()))

    def mousePressEvent(self, event):
        if not (self.adjusting and self.project and event.button() == Qt.MouseButton.LeftButton):
            return super().mousePressEvent(event)
        pos = self._to_canvas(event.position())
        for key in ("dialogue", "speaker", "portrait"):
            initial = getattr(self.project.template, key)
            rect = as_rect(initial)
            handle = QRectF(rect.right() - 27, rect.bottom() - 27, 48, 48)
            if handle.contains(pos) or rect.contains(pos):
                resizing = handle.contains(pos)
                self.active = key
                self.drag = (key, initial, pos, resizing)
                self.setCursor(Qt.CursorShape.SizeFDiagCursor if resizing else Qt.CursorShape.SizeAllCursor)
                self.update()
                return

    def mouseMoveEvent(self, event):
        if not self.drag:
            return super().mouseMoveEvent(event)
        key, initial, origin, resizing = self.drag
        delta = self._to_canvas(event.position()) - origin
        dx, dy = round(delta.x()), round(delta.y())
        if resizing:
            changed = replace(initial, width=min(WIDTH - initial.x, initial.width + dx),
                              height=min(HEIGHT - initial.y, initial.height + dy))
        else:
            changed = replace(initial, x=initial.x + dx, y=initial.y + dy)
        setattr(self.project.template, key, changed.bounded(*TEMPLATE_MINIMUMS[key]))
        self.update()

    def mouseReleaseEvent(self, event):
        if self.drag:
            key, initial, _, _ = self.drag
            self.drag = None
            self.unsetCursor()
            if getattr(self.project.template, key) != initial:
                self.template_changed.emit()
            return
        super().mouseReleaseEvent(event)
