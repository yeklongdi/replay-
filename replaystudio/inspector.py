from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QImageReader
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QScrollArea,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .model import MAX_CUE_SECONDS, Cue, Initiative, Project, ReplayError, Role, Turn, new_id
from .storage import IMAGE_EXTENSIONS, import_asset, open_template, save_template
from .widgets import MARKERS, button


@contextmanager
def blocked(*widgets):
    blockers = [QSignalBlocker(widget) for widget in widgets]
    try:
        yield
    finally:
        for blocker in blockers:
            blocker.unblock()


def label(text: str, style: str = "") -> QLabel:
    item = QLabel()
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setText(text)
    if style:
        item.setObjectName(style)
    return item


def row(*items) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(6)
    for item in items:
        layout.addWidget(item)
    return layout


def number(lower: int, upper: int, suffix: str = "") -> QSpinBox:
    control = QSpinBox()
    control.setRange(lower, upper)
    control.setSuffix(suffix)
    control.setKeyboardTracking(False)
    return control


class Inspector(QTabWidget):
    changed = Signal(str)
    command = Signal(str)
    guides_changed = Signal(bool)
    notice = Signal(str)

    def __init__(self):
        super().__init__()
        self.project: Project | None = None
        self.media: Path | None = None
        self.selection: list[int] = []
        self._dialogue_tab()
        self._roles_tab()
        self._template_tab()
        self._battle_tab()
        self.currentChanged.connect(self._guides)
        self.setEnabled(False)

    def _tab(self, title: str) -> QVBoxLayout:
        content = QWidget()
        content.setObjectName("inspectorBody")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 11, 12, 12)
        layout.setSpacing(9)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        self.addTab(scroll, title)
        return layout

    def _dialogue_tab(self) -> None:
        layout = self._tab("台词")
        self.selection_label = label("在时间线上选择台词", "sectionLabel")
        self.origin = label("", "muted")
        layout.addWidget(self.selection_label)
        layout.addWidget(self.origin)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("台词或过场正文")
        self.text.setMinimumHeight(140)
        self.text.textChanged.connect(self._text_edited)
        layout.addWidget(self.text, 1)
        self.duration = QDoubleSpinBox()
        self.duration.setDecimals(2)
        self.duration.setRange(0.0, MAX_CUE_SECONDS)
        self.duration.setSingleStep(.5)
        self.duration.setSuffix(" 秒")
        self.duration.setSpecialValueText("自动")
        self.duration.setKeyboardTracking(False)
        self.duration.valueChanged.connect(self._duration_edited)
        form = QFormLayout()
        form.addRow("显示时长", self.duration)
        layout.addLayout(form)
        self.assignee = QComboBox()
        self.assign = button("分配角色")
        self.assign.clicked.connect(self._assign)
        layout.addLayout(row(self.assignee, self.assign))
        self.split_button = button("按光标拆分")
        self.split_button.clicked.connect(lambda: self.command.emit("split"))
        self.merge_button = button("合并所选")
        self.merge_button.clicked.connect(lambda: self.command.emit("merge"))
        layout.addLayout(row(self.split_button, self.merge_button))
        raw = button("查看原始日志", "quiet")
        raw.clicked.connect(lambda: self.command.emit("source"))
        layout.addWidget(raw)

    def _roles_tab(self) -> None:
        layout = self._tab("角色")
        self.roles = QComboBox()
        self.roles.currentIndexChanged.connect(self._role_detail)
        add = button("添加 NPC")
        add.clicked.connect(self._new_role)
        layout.addLayout(row(self.roles, add))
        self.role_name = QLineEdit()
        self.role_name.setPlaceholderText("角色显示名")
        self.role_name.editingFinished.connect(self._rename_role)
        layout.addWidget(label("显示名", "fieldTitle"))
        layout.addWidget(self.role_name)
        self.role_origin = label("", "muted")
        layout.addWidget(self.role_origin)
        self.portrait_name = label("", "fileLabel")
        self.portrait_name.setWordWrap(True)
        layout.addWidget(label("立绘", "fieldTitle"))
        layout.addWidget(self.portrait_name)
        choose, self.drop_portrait = button("选择立绘"), button("移除", "quiet")
        choose.clicked.connect(self._choose_portrait)
        self.drop_portrait.clicked.connect(self._remove_portrait)
        layout.addLayout(row(choose, self.drop_portrait))
        layout.addStretch()

    def _template_tab(self) -> None:
        layout = self._tab("模板")
        self.template_tab_index = self.count() - 1
        self.template_name = QLineEdit()
        self.template_name.editingFinished.connect(self._rename_template)
        layout.addWidget(self.template_name)
        self.guides = QCheckBox("在画布上拖动或缩放元素")
        self.guides.setChecked(True)
        self.guides.toggled.connect(self._guides)
        layout.addWidget(self.guides)
        form = QFormLayout()
        form.setSpacing(8)
        properties = (
            ("台词字号", "body_size", 18, 80, " px"),
            ("姓名字号", "speaker_size", 18, 76, " px"),
            ("对话框不透明度", "box_opacity", 0, 100, " %"),
            ("背景不透明度", "background_opacity", 0, 100, " %"),
        )
        self.template_numbers = {}
        for caption, key, minimum, maximum, suffix in properties:
            control = number(minimum, maximum, suffix)
            self.template_numbers[key] = control
            control.valueChanged.connect(lambda value, property_name=key: self._style_number(property_name, value))
            form.addRow(caption, control)
        self.speed = number(1, 40, " 字/秒")
        self.speed.valueChanged.connect(self._speed_changed)
        form.addRow("阅读速度", self.speed)
        layout.addLayout(form)
        self.colors = {}
        colors = QHBoxLayout()
        for caption, key in (("台词色", "body_color"), ("姓名色", "speaker_color"), ("对话框色", "box_color")):
            control = button(caption, "compact")
            control.clicked.connect(lambda checked=False, property_name=key: self._choose_color(property_name))
            self.colors[key] = control
            colors.addWidget(control)
        layout.addLayout(colors)
        save, use = button("保存模板"), button("载入模板")
        save.clicked.connect(self._save_template)
        use.clicked.connect(self._open_template)
        layout.addLayout(row(save, use))
        layout.addStretch()

    def _battle_tab(self) -> None:
        layout = self._tab("战斗")
        start, end = button("标记战斗开始"), button("标记战斗结束")
        start.clicked.connect(lambda: self.command.emit("battle_start"))
        end.clicked.connect(lambda: self.command.emit("battle_end"))
        layout.addLayout(row(start, end))
        self.battle_target = label("选择一条台词编辑先攻", "sectionLabel")
        layout.addWidget(self.battle_target)
        self.turns = QPlainTextEdit()
        self.turns.setPlaceholderText("每行填写一个名称和点数\n例如　守卫：15")
        self.turns.setMinimumHeight(155)
        layout.addWidget(self.turns, 1)
        self.current_turn = QLineEdit()
        self.current_turn.setPlaceholderText("当前行动者名称")
        layout.addWidget(self.current_turn)
        self.apply_battle, self.drop_battle = button("应用先攻"), button("移除先攻", "quiet")
        self.apply_battle.clicked.connect(self._apply_battle)
        self.drop_battle.clicked.connect(self._clear_battle)
        layout.addLayout(row(self.apply_battle, self.drop_battle))

    def bind(self, project: Project, directory: Path) -> None:
        self.project, self.media = project, directory
        self.setEnabled(True)
        self.refresh_roles()
        self.refresh_template()
        self.set_selection([])
        self.setCurrentIndex(0)
        self._guides()

    def set_selection(self, indexes: list[int]) -> None:
        self.selection = indexes
        if self.project is None:
            return
        cue = self.single()
        self.selection_label.setText(
            MARKERS.get(cue.kind, "编辑台词") if cue else
            f"已选择 {len(indexes):,} 项" if indexes else "在时间线上选择台词"
        )
        self.origin.setText(cue.stamp if cue and cue.stamp else "")
        text_cue = cue if cue and cue.is_text else None
        with blocked(self.text, self.duration, self.assignee):
            self.text.setPlainText(text_cue.text if text_cue else "")
            self.duration.setValue(text_cue.duration if text_cue else 0.0)
            if text_cue:
                self.assignee.setCurrentIndex(max(0, self.assignee.findData(text_cue.role)))
        self.text.setEnabled(text_cue is not None)
        self.duration.setEnabled(text_cue is not None)
        self.split_button.setEnabled(text_cue is not None and bool(text_cue.text))
        self.merge_button.setEnabled(len(indexes) > 1)
        any_text = any(self.project.cues[i].is_text for i in indexes)
        self.assignee.setEnabled(any_text)
        self.assign.setEnabled(any_text)
        self.battle_target.setText("所选台词的先攻" if text_cue else "选择一条台词编辑先攻")
        data = text_cue.initiative if text_cue else None
        self.turns.setPlainText("\n".join(f"{turn.name}：{turn.score}" for turn in data.turns) if data else "")
        self.current_turn.setText(data.current if data else "")
        for control in (self.turns, self.current_turn, self.apply_battle):
            control.setEnabled(text_cue is not None)
        self.drop_battle.setEnabled(data is not None)

    def single(self) -> Cue | None:
        if self.project and len(self.selection) == 1:
            return self.project.cues[self.selection[0]]
        return None

    def split_offset(self) -> int:
        text = self.text.toPlainText()
        utf16_offset = self.text.textCursor().position()
        return len(text.encode("utf-16-le")[:utf16_offset * 2].decode("utf-16-le", errors="ignore"))

    def _text_edited(self) -> None:
        if (cue := self.single()) and cue.is_text:
            cue.text = self.text.toPlainText()
            self.split_button.setEnabled(bool(cue.text))
            self.changed.emit("text")

    def _duration_edited(self, value: float) -> None:
        if (cue := self.single()) and cue.is_text:
            cue.duration = value
            self.changed.emit("text")

    def _assign(self) -> None:
        role = self.assignee.currentData()
        for i in self.selection:
            if self.project.cues[i].is_text:
                self.project.cues[i].role = role
        self.changed.emit("assignment")

    def refresh_roles(self, selected: str | None = None) -> None:
        if not self.project:
            return
        role_choice = selected or self.roles.currentData()
        assignment = self.assignee.currentData()
        with blocked(self.roles, self.assignee):
            self.roles.clear()
            self.assignee.clear()
            self.assignee.addItem("（旁白）", "")
            for role in self.project.roles.values():
                self.roles.addItem(role.name, role.id)
                self.assignee.addItem(role.name, role.id)
            self.roles.setCurrentIndex(max(0, self.roles.findData(role_choice)))
            self.assignee.setCurrentIndex(max(0, self.assignee.findData(assignment)))
        self._role_detail()

    def _role_detail(self) -> None:
        if not self.project or not (role := self.project.roles.get(self.roles.currentData())):
            return
        self.role_name.setText(role.name)
        self.role_origin.setText("从日志导入的角色" if role.account else "手动添加的角色")
        portrait = self.project.assets.get(role.portrait)
        self.portrait_name.setText(portrait.name if portrait else "使用默认角色名牌")
        self.portrait_name.setToolTip(portrait.name if portrait else "")
        self.drop_portrait.setEnabled(portrait is not None)

    def _new_role(self) -> None:
        name, ok = QInputDialog.getText(self, "添加 NPC", "角色显示名")
        if ok and name.strip():
            role = Role(new_id(), name.strip())
            self.project.roles[role.id] = role
            self.refresh_roles(role.id)
            self.changed.emit("roles")

    def _rename_role(self) -> None:
        if role := self.project.roles.get(self.roles.currentData()):
            name = self.role_name.text().strip()
            if name and name != role.name:
                role.name = name
                self.refresh_roles(role.id)
                self.changed.emit("roles")
            else:
                self.role_name.setText(role.name)

    def _choose_portrait(self) -> None:
        role = self.project.roles.get(self.roles.currentData())
        if role is None:
            return
        selected, _ = QFileDialog.getOpenFileName(self, "选择立绘", "", "图片 (*.png *.jpg *.jpeg)")
        if not selected:
            return
        try:
            if not QImageReader(selected).canRead():
                raise ReplayError("无法读取这张图片。")
            asset = import_asset(self.project, Path(selected), self.media, IMAGE_EXTENSIONS)
        except (OSError, ReplayError) as error:
            QMessageBox.warning(self, "素材导入失败", str(error))
            return
        role.portrait = asset.id
        self._role_detail()
        self.changed.emit("asset")

    def _remove_portrait(self) -> None:
        if role := self.project.roles.get(self.roles.currentData()):
            role.portrait = ""
            self._role_detail()
            self.changed.emit("asset")

    def _guides(self) -> None:
        self.guides_changed.emit(self.currentIndex() == self.template_tab_index and self.guides.isChecked())

    def refresh_template(self) -> None:
        template = self.project.template
        self.template_name.setText(template.name)
        with blocked(*self.template_numbers.values(), self.speed):
            for key, control in self.template_numbers.items():
                control.setValue(getattr(template, key))
            self.speed.setValue(self.project.reading_speed)
        for key, control in self.colors.items():
            color = QColor(getattr(template, key))
            foreground = "#142423" if color.lightness() > 140 else "#f0f5ef"
            control.setStyleSheet(f"background-color: {color.name()}; color: {foreground};")

    def _rename_template(self) -> None:
        template = self.project.template
        name = self.template_name.text().strip()
        if name and name != template.name:
            template.name = name
            self.changed.emit("template")
        else:
            self.template_name.setText(template.name)

    def _style_number(self, key: str, value: int) -> None:
        setattr(self.project.template, key, value)
        self.changed.emit("template")

    def _speed_changed(self, value: int) -> None:
        self.project.reading_speed = value
        self.changed.emit("template")

    def _choose_color(self, key: str) -> None:
        color = QColorDialog.getColor(QColor(getattr(self.project.template, key)), self, "选择颜色")
        if color.isValid():
            setattr(self.project.template, key, color.name())
            self.refresh_template()
            self.changed.emit("template")

    def _save_template(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self, "保存可复用模板", "回放模板.json", "Replay 模板 (*.json)"
        )
        if selected:
            path = Path(selected)
            if path.suffix.lower() != ".json":
                path = Path(str(path) + ".json")
            try:
                save_template(self.project.template, path)
                self.notice.emit("模板已保存")
            except (OSError, ReplayError) as error:
                QMessageBox.warning(self, "模板保存失败", str(error))

    def _open_template(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "载入模板", "", "Replay 模板 (*.json)")
        if selected:
            try:
                template = open_template(Path(selected))
            except (OSError, ReplayError) as error:
                QMessageBox.warning(self, "模板载入失败", str(error))
                return
            self.project.template = template
            self.refresh_template()
            self.changed.emit("template")

    def _apply_battle(self) -> None:
        cue = self.single()
        if not cue or not cue.is_text:
            return
        turns = []
        for line in self.turns.toPlainText().splitlines():
            if line.strip():
                parts = re.split(r"[：:]", line, maxsplit=1)
                name = parts[0].strip()
                if not name:
                    QMessageBox.warning(self, "先攻需要角色名", "请在点数前填写角色名。")
                    return
                turns.append(Turn(name, parts[1].strip() if len(parts) == 2 else ""))
        cue.initiative = Initiative(tuple(turns), self.current_turn.text().strip())
        self.drop_battle.setEnabled(True)
        self.changed.emit("battle")

    def _clear_battle(self) -> None:
        if cue := self.single():
            cue.initiative = None
            self.set_selection(self.selection)
            self.changed.emit("battle")
