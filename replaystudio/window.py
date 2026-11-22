from __future__ import annotations

import tempfile
from bisect import bisect_right
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QInputDialog, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QSlider, QSplitter, QStackedWidget, QStatusBar, QTabBar, QTableView, QVBoxLayout, QWidget,
)

from .appearance import DEFAULT_TONE, TONE_NAMES, apply_tone
from .audio import AudioCache, PreparationCancelled, PreviewAudio, voice_as_media_clip
from .exporting import ExportCancelled, ExportJob
from .inspector import Inspector, blocked, label, row
from .media_editor import MediaEditor
from .model import FPS, MAX_MEDIA_FRAME, Project, ReplayError, project_from_log
from .render import Timeline, build_timeline, initialize_media
from .storage import PROJECT_SUFFIX, open_project, save_project
from .voice_editor import VoiceEditor
from .widgets import CueFilter, CueModel, Stage, app_icon, button, panel, timecode

LOG_SUFFIXES = (".txt", ".docx")
LOG_FILES = "跑团日志 (" + " ".join(f"*{suffix}" for suffix in LOG_SUFFIXES) + ")"
PROJECT_FILES = "Replay 工程 (*.replay)"


class ExportWorker(QThread):
    progress = Signal(int, str)

    def __init__(self, project, timeline, media, start, end, output, parent):
        super().__init__(parent)
        self.job = ExportJob(project, timeline, media, start, end, output, self.progress.emit)
        self.error = ""
        self.cancelled = False

    def run(self):
        try:
            self.job.run()
        except ExportCancelled:
            self.cancelled = True
        except Exception as error:
            self.error = str(error) or type(error).__name__


class ExportDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("导出 MP4")
        self.setMinimumWidth(390)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 16)
        self.note = label("准备导出", "sectionLabel")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.cancel_button = button("取消导出", "quiet")
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.note)
        layout.addWidget(self.bar)
        layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignRight)
        self.worker: ExportWorker | None = None

    def watch(self, worker: ExportWorker) -> None:
        self.worker = worker
        worker.progress.connect(self.update_progress)
        worker.finished.connect(self.accept)
        QTimer.singleShot(0, worker.start)
        self.exec()
        worker.wait()

    def update_progress(self, percent: int, message: str) -> None:
        if self.cancel_button.isEnabled():
            self.bar.setValue(percent)
            self.note.setText(message)

    def reject(self) -> None:
        if self.worker is not None:
            self.worker.job.cancel()
            self.cancel_button.setEnabled(False)
            self.note.setText("正在结束编码并清理临时文件")
        else:
            super().reject()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("回放工坊")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(890, 480)
        screen = self.screen().availableGeometry()
        self.resize(min(1500, screen.width() - 40), min(920, screen.height() - 65))
        self.setAcceptDrops(True)
        self.project: Project | None = None
        self.media: tempfile.TemporaryDirectory | None = None
        self.project_file: Path | None = None
        self.last_dir = Path.home()
        self.dirty = False
        self.timeline = Timeline([], [], {}, 0)
        self.frame = 0
        self.playing = False
        self._clock = 0.0
        self._play_first = 0
        self._play_end = 0
        self._anchors: list[tuple[str, int] | None] = [None, None]
        self._resetting = False
        self._period_summary = ""
        self._guides = False
        self._tracks_height = 452

        self.update_timer = QTimer(self)
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(120)
        self.update_timer.timeout.connect(self._rebuild)
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(110)
        self.filter_timer.timeout.connect(self._filter)
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(25)
        self.play_timer.timeout.connect(self._tick)
        self.audio = PreviewAudio(self)
        self.audio.error.connect(lambda message: self._status(f"音频预览失败　{message}"))
        self._build_ui()
        self._enable_project_controls(False)

    def _build_ui(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        workspace_bar = QFrame()
        workspace_bar.setObjectName("workspaceBar")
        tabs = QHBoxLayout(workspace_bar)
        tabs.setContentsMargins(13, 2, 13, 0)
        tabs.setSpacing(6)
        self.settings_button = self._settings_button()
        tabs.addWidget(self.settings_button)
        self.workspace_tabs = QTabBar()
        self.workspace_tabs.setExpanding(False)
        self.workspace_tabs.setDrawBase(False)
        self.workspace_tabs.addTab("台词")
        self.workspace_tabs.addTab("剪辑")
        self.workspace_tabs.addTab("声音")
        tabs.addWidget(self.workspace_tabs)
        tabs.addStretch()
        outer.addWidget(workspace_bar)

        self.media_editor = MediaEditor(self)
        self.media_editor.seek.connect(self._seek)
        self.media_editor.notice.connect(self._status)
        self.media_editor.changed.connect(self._media_changed)

        self.voice_editor = VoiceEditor(self.audio, self)
        self.voice_editor.seek.connect(self._seek)
        self.voice_editor.notice.connect(self._status)
        self.voice_editor.changed.connect(self._voice_changed)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setContentsMargins(0, 0, 0, 0)
        split.setChildrenCollapsible(False)
        self.left_stack = QStackedWidget()
        self.left_stack.addWidget(self._left())
        self.left_stack.addWidget(self.media_editor.library_panel)
        self.left_stack.addWidget(self.voice_editor.left_panel)
        split.addWidget(self.left_stack)
        split.addWidget(self._center())

        right = panel()
        right.setMinimumWidth(268)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(1, 1, 1, 2)
        self.inspector = Inspector()
        self.inspector.changed.connect(self._changed)
        self.inspector.command.connect(self._command)
        self.inspector.guides_changed.connect(self._edit_guides)
        self.inspector.notice.connect(self._status)
        right_layout.addWidget(self.inspector)
        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(right)
        self.right_stack.addWidget(self.media_editor.details_panel)
        self.right_stack.addWidget(self.voice_editor.right_panel)
        split.addWidget(self.right_stack)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([385, 740, 335])

        self.work_split = QSplitter(Qt.Orientation.Vertical)
        self.work_split.setContentsMargins(10, 4, 10, 2)
        self.work_split.setChildrenCollapsible(False)
        self.work_split.addWidget(split)
        self.work_split.addWidget(self.media_editor.timeline_panel)
        self.work_split.setStretchFactor(0, 1)
        self.work_split.setStretchFactor(1, 0)
        self.work_split.splitterMoved.connect(self._remember_tracks_height)
        self.media_editor.timeline_panel.hide()
        outer.addWidget(self.work_split, 1)
        self.workspace_tabs.currentChanged.connect(self._switch_workspace)
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().setFixedHeight(18)
        self.choose_log_button.setFocus()

    def _remember_tracks_height(self) -> None:
        self._tracks_height = self.work_split.sizes()[1]

    def _switch_workspace(self, index: int) -> None:
        self._ensure_rebuilt()
        self.left_stack.setCurrentIndex(index)
        self.right_stack.setCurrentIndex(index)
        self.setMinimumHeight(604 if index == 1 else 480)
        self.media_editor.timeline_panel.setVisible(index == 1)
        self.voice_editor.cues_panel.setVisible(index == 2)
        if index == 1:
            self.center_range_slot.removeWidget(self.range_bar)
            self.media_editor.range_divider.show()
            self.media_editor.range_slot.addWidget(self.range_bar)
            self.range_bar.show()
            available = self.work_split.contentsRect().height() - self.work_split.handleWidth()
            height = min(self._tracks_height, max(175, available - 245))
            self.work_split.setSizes([available - height, height])
            self.media_editor.timeline.set_position(self.frame, reveal=True)
        else:
            self.media_editor.range_slot.removeWidget(self.range_bar)
            self.media_editor.range_divider.hide()
            self.center_range_slot.addWidget(self.range_bar)
            self.range_bar.setVisible(index == 0)
            if index == 2 and self.project:
                self.voice_editor.refresh()
        self._edit_guides(self._guides)

    def _settings_button(self) -> QWidget:
        control = button("设置", "settings")
        menu = QMenu(control)

        def command(caption, slot, shortcut=""):
            action = menu.addAction(caption, slot)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
                action.setShortcutVisibleInContextMenu(True)
            self.addAction(action)
            return action

        self.import_action = command("导入日志", self._choose_log, "Ctrl+I")
        self.open_action = command("打开工程", self._choose_project, "Ctrl+O")
        self.save_action = command("保存", self._save, "Ctrl+S")
        self.package_action = command("打包工程", self._package)
        self.export_action = command("导出 MP4", self._export, "Ctrl+Shift+E")
        menu.addSeparator()
        menu.addAction("界面色调").setEnabled(False)
        group = QActionGroup(menu)
        current = QApplication.instance().property("replayTone") or DEFAULT_TONE
        for key, name in TONE_NAMES.items():
            action = menu.addAction(name)
            action.setData(key)
            action.setCheckable(True)
            group.addAction(action)
            action.setChecked(key == current)
        group.triggered.connect(lambda action: apply_tone(QApplication.instance(), action.data(), remember=True))
        control.setMenu(menu)
        return control

    def _left(self) -> QWidget:
        frame = panel()
        frame.setMinimumWidth(240)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(11, 10, 11, 10)
        layout.setSpacing(8)
        layout.addWidget(label("台词时间线", "panelTitle"))
        self.query = QLineEdit()
        self.query.setPlaceholderText("搜索台词、角色或时间")
        self.query.setClearButtonEnabled(True)
        self.query.textChanged.connect(lambda: self.filter_timer.start())
        layout.addWidget(self.query)
        self.role_filter = QComboBox()
        self.date_filter = QComboBox()
        self.visibility = QComboBox()
        for text, key in (("所有记录", ""), ("可播放", "shown"), ("已隐藏", "hidden"), ("空记录", "empty")):
            self.visibility.addItem(text, key)
        self.role_filter.addItem("全部角色", "")
        self.date_filter.addItem("不限日期", "")
        for filter_box in (self.role_filter, self.date_filter, self.visibility):
            filter_box.setMinimumContentsLength(4)
            filter_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            filter_box.currentIndexChanged.connect(self._filter)
        layout.addLayout(row(self.role_filter, self.date_filter, self.visibility))
        self.feed = label("等待导入", "meta")
        layout.addWidget(self.feed)

        self.model = CueModel(self)
        self.proxy = CueFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 78)
        self.table.setColumnWidth(1, 83)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_timeline_menu)
        delete_key = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.table)
        delete_key.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete_key.activated.connect(self._delete_selected)
        layout.addWidget(self.table, 1)

        self.add_button = button("补写台词", "compact")
        self.interlude_button = button("插入过场", "compact")
        self.hide_button = button("隐藏", "compact")
        self.restore_button = button("恢复", "compact")
        self.up_button = button("上移", "compact")
        self.down_button = button("下移", "compact")
        for control, slot in (
            (self.add_button, lambda: self._command("dialogue")),
            (self.interlude_button, lambda: self._command("interlude")),
            (self.hide_button, lambda: self._hide(True)),
            (self.restore_button, lambda: self._hide(False)),
            (self.up_button, lambda: self._move(-1)), (self.down_button, lambda: self._move(1)),
        ):
            control.clicked.connect(slot)
        layout.addLayout(row(self.add_button, self.interlude_button))
        layout.addLayout(row(self.hide_button, self.restore_button, self.up_button, self.down_button))
        return frame

    def _center(self) -> QWidget:
        frame = panel()
        frame.setMinimumWidth(330)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 9, 10, 8)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        heading.addWidget(label("预览画面", "panelTitle"))
        heading.addStretch()
        self.scene_indicator = label("1080p · 30 帧/秒", "meta")
        self.scene_indicator.setMaximumWidth(320)
        heading.addWidget(self.scene_indicator)
        layout.addLayout(heading)

        self.stage = Stage()
        self.stage.template_changed.connect(lambda: self._changed("template"))
        self.stage_stack = QStackedWidget()
        empty = QWidget()
        empty.setObjectName("emptyStage")
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(12, 16, 12, 16)
        empty_layout.setSpacing(12)
        empty_layout.addStretch()
        badge = label("")
        badge.setPixmap(app_icon().pixmap(64, 64))
        badge.setFixedSize(64, 64)
        empty_layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignHCenter)
        title = label("拖入跑团日志", "panelTitle")
        title.setStyleSheet("font-size: 20px;")
        empty_layout.addWidget(title, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addWidget(label("UTF-8 TXT、DOCX 日志或 .replay 工程", "muted"),
                               0, Qt.AlignmentFlag.AlignHCenter)
        self.choose_log_button = button("选择日志文件", "accent")
        self.choose_log_button.clicked.connect(self._choose_log)
        empty_layout.addWidget(self.choose_log_button, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch()
        self.stage_stack.addWidget(empty)
        self.stage_stack.addWidget(self.stage)
        layout.addWidget(self.stage_stack, 1)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setSingleStep(1)
        self.slider.setFixedHeight(22)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(self._pause)
        self.slider.valueChanged.connect(self._seek_slider)
        layout.addWidget(self.slider)
        transport = QHBoxLayout()
        transport.setSpacing(6)
        self.play_button = button("播放", "play")
        self.prev_button = button("上一屏", "compact")
        self.next_button = button("下一屏", "compact")
        self.play_button.clicked.connect(self._toggle_play)
        self.prev_button.clicked.connect(lambda: self._step(-1))
        self.next_button.clicked.connect(lambda: self._step(1))
        transport.addWidget(self.play_button)
        transport.addWidget(self.prev_button)
        transport.addWidget(self.next_button)
        transport.addStretch()
        self.clock_label = label("00:00:00 / 00:00:00", "time")
        transport.addWidget(self.clock_label)
        layout.addLayout(transport)
        self.range_bar = QWidget()
        controls = QHBoxLayout(self.range_bar)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        self.start_button = button("标记起点", "compact")
        self.end_button = button("标记终点", "compact")
        self.range_play = button("播放选段", "compact")
        self.range_clear = button("清除", "compact")
        self.start_button.setToolTip("把当前帧设为选段的第一帧")
        self.end_button.setToolTip("把当前帧设为选段的最后一帧")
        self.start_button.clicked.connect(lambda: self._mark(0))
        self.end_button.clicked.connect(lambda: self._mark(1))
        self.range_play.clicked.connect(self._play_range)
        self.range_clear.clicked.connect(self._clear_range)
        self.range_label = label("导出范围　整集", "rangeLabel")
        for control in (self.start_button, self.end_button, self.range_play, self.range_clear, self.range_label):
            controls.addWidget(control)
        controls.addStretch()
        self.center_range_slot = QVBoxLayout()
        self.center_range_slot.setContentsMargins(0, 0, 0, 0)
        self.center_range_slot.addWidget(self.range_bar)
        layout.addLayout(self.center_range_slot)
        layout.addWidget(self.voice_editor.cues_panel)
        return frame

    def _status(self, message: str) -> None:
        self.statusBar().showMessage(message, 6000)

    def _enable_project_controls(self, yes: bool) -> None:
        for control in (self.save_action, self.package_action, self.query, self.role_filter,
                        self.date_filter, self.visibility, self.table, self.add_button,
                        self.interlude_button):
            control.setEnabled(yes)
        self.workspace_tabs.setTabEnabled(1, yes)
        self.workspace_tabs.setTabEnabled(2, yes)
        self._enable_playback()
        self._selection_actions([])

    def _enable_playback(self) -> None:
        ready = self.timeline.length > 0
        for control in (self.export_action, self.slider, self.play_button, self.prev_button,
                        self.next_button, self.start_button, self.end_button):
            control.setEnabled(ready)
        self._update_range()

    def _update_project_heading(self) -> None:
        if self.project:
            title = self.project.title
            self.setWindowTitle(f"{title}[*] · 回放工坊")
            self.setWindowModified(self.dirty)

    def _confirm_replace(self) -> bool:
        self.media_editor.commit_input()
        if not self.dirty:
            return True
        choice = QMessageBox.question(
            self, "保存工程", "当前工程有修改。要保存后继续吗？",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Save:
            return self._save()
        return choice == QMessageBox.StandardButton.Discard

    def _choose_log(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "导入跑团日志", str(self.last_dir), LOG_FILES)
        if selected:
            self.load_path(Path(selected))

    def _choose_project(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "打开回放工程", str(self.last_dir), PROJECT_FILES)
        if selected:
            self.load_path(Path(selected))

    def load_path(self, filename: Path) -> bool:
        if not self._confirm_replace():
            return False
        workspace = tempfile.TemporaryDirectory(prefix="replay-media-", ignore_cleanup_errors=True)
        project_file = None
        try:
            suffix = filename.suffix.lower()
            if suffix in LOG_SUFFIXES:
                project = project_from_log(filename.read_bytes(), filename.name, filename.stem)
            elif suffix == PROJECT_SUFFIX:
                project = open_project(filename, Path(workspace.name))
                project_file = filename
            else:
                raise ReplayError("请选择 UTF-8 TXT、DOCX 跑团日志或 .replay 工程。")
            initialize_media(project)
        except (OSError, ReplayError) as error:
            workspace.cleanup()
            QMessageBox.warning(self, "打开失败", str(error))
            return False

        self._pause()
        self.update_timer.stop()
        self.filter_timer.stop()
        self.audio.bind(None)
        if self.media:
            self.media.cleanup()
        self.project, self.media, self.project_file = project, workspace, project_file
        self.last_dir = filename.parent
        self.dirty = project_file is None
        self._anchors = [None, None]
        self.frame = 0
        self.timeline = Timeline([], [], {}, 0)
        sessions = sorted({record.session for record in project.messages if record.session})
        if sessions:
            period_label = "不限场次"
            periods = [(f"第 {session} 场", session) for session in sessions]
            self._period_summary = f"{len(sessions)} 场"
        else:
            period_label = "不限日期"
            dates = sorted({record.stamp[:10] for record in project.messages})
            periods = [(date, date) for date in dates]
            self._period_summary = f"{len(dates)} 个日期"
        self._resetting = True
        with blocked(self.query, self.date_filter, self.visibility):
            self.query.clear()
            self.visibility.setCurrentIndex(0)
            self.date_filter.clear()
            self.date_filter.addItem(period_label, "")
            for text, key in periods:
                self.date_filter.addItem(text, key)
        self._role_choices(reset=True)
        self.model.bind(project)
        self._resetting = False
        self.proxy.apply("", "", "", "")
        directory = Path(workspace.name)
        self.stage.bind(project, directory)
        cache = AudioCache(project, directory)
        self.audio.bind(cache)
        self.media_editor.bind(project, cache)
        self.voice_editor.bind(project, cache)
        self.inspector.bind(project, directory)
        self.workspace_tabs.setCurrentIndex(0)
        self.stage_stack.setCurrentWidget(self.stage)
        self._enable_project_controls(True)
        self._rebuild()
        if self.stage.page:
            self._select_ids({self.stage.page.cue_id})
        self.table.setFocus()
        self._update_project_heading()
        empty_count = sum(not record.text for record in project.messages)
        self._status(f"已打开 {filename.name}　{self._period_summary} · {empty_count:,} 条原始空记录")
        return True

    def _save(self) -> bool:
        if not self.project:
            return False
        path = self.project_file or self._save_location("保存工程", self.project.title, PROJECT_SUFFIX, PROJECT_FILES)
        if not path or not self._write_package(path):
            return False
        self.project_file, self.dirty = path, False
        self._update_project_heading()
        self._status(f"工程已保存　{path}")
        return True

    def _save_location(self, title: str, stem: str, suffix: str, filters: str) -> Path | None:
        selected, _ = QFileDialog.getSaveFileName(self, title, str(self.last_dir / (stem + suffix)), filters)
        if not selected:
            return None
        path = Path(selected)
        return path if path.suffix.lower() == suffix else Path(str(path) + suffix)

    def _write_package(self, path: Path) -> bool:
        self.media_editor.commit_input()
        self._ensure_rebuilt()
        try:
            save_project(self.project, path, Path(self.media.name))
        except (OSError, ReplayError) as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return False
        self.last_dir = path.parent
        return True

    def _package(self) -> None:
        if not self.project:
            return
        path = self._save_location("打包完整工程", self.project.title + " - 交接", PROJECT_SUFFIX, PROJECT_FILES)
        if path and self._write_package(path):
            self._status(f"工程包已保存　{path}")

    def _role_choices(self, reset: bool = False) -> None:
        current = "" if reset else self.role_filter.currentData()
        with blocked(self.role_filter):
            self.role_filter.clear()
            self.role_filter.addItem("全部角色", "")
            for role in self.project.roles.values():
                self.role_filter.addItem(role.name, role.id)
            self.role_filter.setCurrentIndex(max(0, self.role_filter.findData(current)))

    def _filter(self) -> None:
        if not self.project or self._resetting:
            return
        self.proxy.apply(self.query.text(), self.role_filter.currentData() or "",
                         self.date_filter.currentData() or "", self.visibility.currentData() or "")
        self._update_count()

    def _update_count(self) -> None:
        if self.project:
            self.feed.setText(f"{self.proxy.rowCount():,} / {len(self.project.cues):,} 项　·　{self._period_summary}")

    def _rows(self) -> list[int]:
        return sorted(self.proxy.mapToSource(index).row() for index in self.table.selectionModel().selectedRows())

    def _show_timeline_menu(self, position) -> None:
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        selection = self.table.selectionModel()
        if not selection.isSelected(index):
            flags = QItemSelectionModel.SelectionFlag
            selection.setCurrentIndex(index, flags.ClearAndSelect | flags.Rows)

        menu = QMenu(self.table)
        for controls in (
            (self.hide_button, self.restore_button, self.up_button, self.down_button),
            (self.inspector.split_button, self.inspector.merge_button),
            (self.add_button, self.interlude_button),
        ):
            for control in controls:
                action = menu.addAction(control.text(), control.click)
                action.setEnabled(control.isEnabled())
            menu.addSeparator()

        roles = menu.addMenu("分配角色")
        roles.setEnabled(self.inspector.assign.isEnabled())
        assignee = self.inspector.assignee
        for i in range(assignee.count()):
            roles.addAction(assignee.itemText(i), lambda checked=False, role=i: self._assign_from_menu(role))

        markers = menu.addMenu("插入标记")
        for caption, command in (("标记战斗开始", "battle_start"),
                                 ("标记战斗结束", "battle_end")):
            markers.addAction(caption, lambda checked=False, name=command: self._command(name))
        menu.addSeparator()
        menu.addAction("删除记录\tDelete", self._delete_selected)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.table.viewport().mapToGlobal(position))

    def _assign_from_menu(self, index: int) -> None:
        self.inspector.assignee.setCurrentIndex(index)
        self.inspector.assign.click()

    def _selection_actions(self, indices: list[int]) -> None:
        selected = [self.project.cues[index] for index in indices] if self.project else []
        self.hide_button.setEnabled(any(not cue.hidden for cue in selected))
        self.restore_button.setEnabled(any(cue.hidden for cue in selected))
        self.up_button.setEnabled(bool(indices and indices[-1] > 0))
        self.down_button.setEnabled(bool(indices and indices[0] < len(self.project.cues) - 1))

    def _selection_changed(self) -> None:
        if self._resetting or not self.project:
            return
        indexes = self._rows()
        self.inspector.set_selection(indexes)
        self._selection_actions(indexes)
        if len(indexes) == 1:
            self._ensure_rebuilt()
            cue = self.project.cues[indexes[0]]
            if (frame := self.timeline.cue_frames.get(cue.id)) is not None:
                self._seek(frame)

    def _select_ids(self, ids: set[str], reveal: bool = False) -> None:
        rows = []
        for source_row, cue in enumerate(self.project.cues):
            if cue.id in ids:
                index = self.proxy.mapFromSource(self.model.index(source_row, 0))
                if index.isValid():
                    rows.append(index)
        if not rows and ids and reveal:
            with blocked(self.query, self.role_filter, self.date_filter, self.visibility):
                self.query.clear()
                self.role_filter.setCurrentIndex(0)
                self.date_filter.setCurrentIndex(0)
                self.visibility.setCurrentIndex(0)
            self._filter()
            self._select_ids(ids)
            return

        select = QItemSelection()
        for index in rows:
            select.select(index, self.proxy.index(index.row(), 2))
        selection = self.table.selectionModel()
        selection.select(select, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        if rows:
            selection.setCurrentIndex(rows[0], QItemSelectionModel.SelectionFlag.NoUpdate)
            self.table.scrollTo(rows[0])
        self._selection_changed()

    def _changed(self, reason: str) -> None:
        self.dirty = True
        self._update_project_heading()
        if reason in ("text", "battle"):
            indexes = self._rows()
            if indexes:
                self.model.update_row(indexes[0])
        elif reason in ("roles", "assignment"):
            self.model.update_all()
            if reason == "roles":
                self._role_choices()
            self._filter()
        if reason == "template":
            self.stage.update()
        if reason == "asset":
            self.stage.update()
        self.update_timer.start()

    def _media_changed(self) -> None:
        self.dirty = True
        self._update_project_heading()
        self._rebuild()

    def _voice_changed(self) -> None:
        self.dirty = True
        self._update_project_heading()
        self.update_timer.start()

    def _structure(self, selection: set[str], reveal: bool = False) -> None:
        self._resetting = True
        self.dirty = True
        self.model.bind(self.project)
        self._resetting = False
        self._rebuild()
        self._select_ids(selection, reveal)
        self._update_project_heading()

    def _hide(self, hidden: bool) -> None:
        selected = [self.project.cues[i] for i in self._rows()]
        for cue in selected:
            cue.hidden = hidden
        self._structure({cue.id for cue in selected})

    def _delete_selected(self) -> None:
        selection = self.table.selectionModel()
        rows = selection.selectedRows()
        if not rows or self.project is None:
            return
        first = min(index.row() for index in rows)
        indices = {self.proxy.mapToSource(index).row() for index in rows}
        self._pause()
        self.project.remove(indices)
        self._structure(set())
        if count := self.proxy.rowCount():
            next_row = self.proxy.index(min(first, count - 1), 0)
            flags = QItemSelectionModel.SelectionFlag
            selection.setCurrentIndex(next_row, flags.ClearAndSelect | flags.Rows)
            self.table.scrollTo(next_row)
        self._status(f"已删除 {len(indices):,} 项记录")

    def _move(self, direction: int) -> None:
        ids = {self.project.cues[i].id for i in self._rows()}
        if ids:
            self.project.move(ids, direction)
            self._structure(ids)

    def _command(self, command: str) -> None:
        if not self.project:
            return
        indices = self._rows()
        before = indices[0] if indices else len(self.project.cues)
        after = indices[-1] + 1 if indices else len(self.project.cues)
        try:
            if command == "source":
                self._show_source()
                return
            if command == "split":
                cue = self.project.split(before, self.inspector.split_offset())
            elif command == "merge":
                cue = self._merge(indices)
                if cue is None:
                    return
            elif command == "battle_start":
                cue = self.project.insert(before, command)
            elif command == "battle_end":
                cue = self.project.insert(after, command)
            else:
                inherited = self.project.cues[indices[-1]].role if indices else ""
                first_role = next(iter(self.project.roles), "")
                cue = self.project.insert(after, command, role=(inherited or first_role) if command == "dialogue" else "")
        except ReplayError as error:
            self._status(str(error))
            return
        self._structure({cue.id}, reveal=True)
        if command in ("dialogue", "interlude"):
            self.inspector.setCurrentIndex(0)
            self.inspector.text.setFocus()

    def _merge(self, indices: list[int]):
        roles = list(dict.fromkeys(self.project.cues[i].role for i in indices))
        if len(roles) > 1:
            labels = [f"{i + 1}. {self.project.roles[role].name if role else '旁白'}"
                      for i, role in enumerate(roles)]
            choice, ok = QInputDialog.getItem(self, "合并台词", "合并后显示的说话人", labels, 0, False)
            if not ok:
                return None
            role = roles[labels.index(choice)]
        else:
            role = roles[0] if roles else ""
        return self.project.merge(indices, role)

    def _show_source(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(self.project.source_name + " · 原始日志")
        dialog.resize(760, 540)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(self.project.source.decode("utf-8-sig"))
        layout.addWidget(view)
        close = button("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()
        dialog.deleteLater()

    def _ensure_rebuilt(self) -> None:
        if self.update_timer.isActive():
            self._rebuild()

    def _rebuild(self) -> None:
        self.update_timer.stop()
        if not self.project:
            return
        anchor = self._anchor(self.frame)
        self._pause()
        self.timeline = build_timeline(self.project)
        self.media_editor.update_timeline(self.timeline)
        self.voice_editor.update_timeline(self.timeline)
        frame = self._resolve(anchor)
        with blocked(self.slider):
            self.slider.setRange(0, min(MAX_MEDIA_FRAME, max(0, self.timeline.length - 1)))
            self.slider.setPageStep(FPS * 3)
        self._display(self.frame if frame is None else frame, seek=True)
        self.stage.update()
        self._enable_playback()
        self._update_count()

    def _display(self, frame: int, seek: bool = False) -> None:
        self.frame = max(0, min(self.timeline.length - 1, frame))
        page = self.timeline.page_at(self.frame)
        self.stage.display(page, self.timeline.media.background_at(self.frame))
        self.media_editor.timeline.set_position(self.frame, reveal=seek or self.playing)
        self.voice_editor.set_current_cue(page.cue_id if page else "")
        extent = max(1, self.timeline.length - 1)
        with blocked(self.slider):
            self.slider.setValue((self.frame * self.slider.maximum() + extent // 2) // extent)
        self._update_clock()
        if page:
            self.scene_indicator.setText(f"第 {page.index + 1}/{page.page_count} 屏")
        elif self.project:
            self.scene_indicator.setText("素材画面" if self.timeline.length else "暂无可播放内容")

    def _update_clock(self) -> None:
        position, total = timecode(self.frame), timecode(self.timeline.length)
        self.clock_label.setText(position if self.width() < 1000 else f"{position} / {total}")
        self.clock_label.setToolTip(f"总时长 {total}")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "clock_label"):
            self._update_clock()

    def _seek_slider(self, value: int) -> None:
        maximum = max(1, self.slider.maximum())
        extent = max(0, self.timeline.length - 1)
        self._seek((value * extent + maximum // 2) // maximum)

    def _seek(self, frame: int) -> None:
        self._pause()
        self._display(frame, seek=True)

    def _pause(self) -> None:
        if not self.playing:
            return
        self.play_timer.stop()
        self.audio.stop()
        self.playing = False
        self.play_button.setText("播放")
        self._edit_guides(self._guides)
        self._display(self.frame)

    def _begin_play(self, start: int, end: int) -> None:
        self._play_first, self._play_end = start, end
        audible = tuple(clip for clip in self.timeline.media.audio if clip.start < end and start < clip.end)
        voices = tuple(span for span in self.timeline.voices if span.start < end and start < span.end)
        try:
            ut_spans = {}
            ut_custom_refs = set()
            asset_refs = [clip.asset for clip in audible]
            for span in voices:
                if span.mode == "ut":
                    ut_spans[span.asset] = span
                    if (span.role_voice and span.role_voice.ut_preset == "custom"
                            and span.role_voice.ut_asset in self.project.assets):
                        ut_custom_refs.add(span.role_voice.ut_asset)
                else:
                    asset_refs.append(span.asset)
            self.media_editor.prepare_audio([*asset_refs, *ut_custom_refs])
            ut_custom_files = {
                ref: self.audio.cache.cached(ref)
                for ref in ut_custom_refs if self.audio.cache.cached(ref) is not None
            }
        except PreparationCancelled:
            self._status("已取消准备预览音频")
            return
        except (OSError, ReplayError) as error:
            self._status(f"音频预览失败　{error}")
        else:
            combined = (*audible, *(voice_as_media_clip(span) for span in voices))
            music_tracks = frozenset(
                track.id for track in self.project.media.tracks if track.kind == "music"
            ) if self.project and self.project.media else frozenset({"music"})
            duck_gain = (
                self.project.voice.duck_level / 100
                if self.project and self.project.voice.duck_music else 1.0
            )
            self.audio.play(
                combined, start, end,
                duck_spans=self.timeline.duck_spans,
                duck_tracks=music_tracks,
                duck_gain=duck_gain,
                ut_spans=ut_spans,
                ut_custom_files=ut_custom_files,
            )
        self._clock = perf_counter()
        self.playing = True
        self.play_button.setText("暂停")
        self._edit_guides(self._guides)
        self._display(start, seek=True)
        self.play_timer.start()

    def _toggle_play(self) -> None:
        if self.playing:
            self._pause()
            return
        self._ensure_rebuilt()
        if self.timeline.length:
            start = 0 if self.frame >= self.timeline.length - 1 else self.frame
            self._begin_play(start, self.timeline.length)

    def _tick(self) -> None:
        now = self.audio.step()
        if now is None:
            now = self._play_first + int((perf_counter() - self._clock) * FPS)
        else:
            self._play_first = now
            self._clock = perf_counter()
        if now >= self._play_end:
            self._display(self._play_end - 1)
            self._pause()
        else:
            self._display(now)

    def _step(self, direction: int) -> None:
        self._ensure_rebuilt()
        if boundaries := self.timeline.visual_starts:
            current = bisect_right(boundaries, self.frame) - 1
            index = max(0, min(len(boundaries) - 1, current + direction))
            self._seek(boundaries[index])

    def _edit_guides(self, active: bool) -> None:
        self._guides = active
        self.stage.adjusting = active and not self.playing and self.workspace_tabs.currentIndex() == 0
        self.stage.update()

    def _anchor(self, frame: int) -> tuple[str, int] | None:
        if page := self.timeline.page_at(frame):
            return page.cue_id, frame - self.timeline.cue_frames[page.cue_id]
        return ("", frame) if 0 <= frame < self.timeline.length else None

    def _resolve(self, anchor: tuple[str, int] | None) -> int | None:
        if not anchor:
            return None
        cue_id, offset = anchor
        if not cue_id:
            return offset if offset < self.timeline.length else None
        if (start := self.timeline.cue_frames.get(cue_id)) is None:
            return None
        index = self.timeline.index_at(start)
        first = self.timeline.pages[index]
        end = self.timeline.pages[index + first.page_count - 1].end
        return min(start + offset, end - 1)

    def _range(self) -> tuple[int, int] | None:
        start, last = (self._resolve(anchor) for anchor in self._anchors)
        if start is not None and last is not None and last >= start:
            return start, last + 1
        return None

    def _mark(self, which: int) -> None:
        self._ensure_rebuilt()
        if self.timeline.length:
            self._anchors[which] = self._anchor(self.frame)
            self._update_range()

    def _clear_range(self) -> None:
        self._anchors = [None, None]
        self._update_range()

    def _update_range(self) -> None:
        selected = self._range()
        self.range_play.setEnabled(selected is not None)
        self.range_clear.setEnabled(any(self._anchors))
        if selected:
            start, end = selected
            text = f"选段　{timecode(start, True)} → {timecode(end, True)}"
        elif any(self._anchors):
            start, last = (self._resolve(anchor) for anchor in self._anchors)
            left = timecode(start, True) if start is not None else "未设"
            right = timecode(last + 1, True) if last is not None else "未设"
            text = f"选段　{left} → {right}　请确认起止顺序"
        else:
            text = "导出范围　整集"
        self.range_label.setText(text)
        self.range_label.setToolTip(text)
        marked = bool(any(self._anchors))
        if self.range_label.property("marked") != marked:
            self.range_label.setProperty("marked", marked)
            self.range_label.style().unpolish(self.range_label)
            self.range_label.style().polish(self.range_label)

    def _play_range(self) -> None:
        self._ensure_rebuilt()
        if selected := self._range():
            self._pause()
            self._begin_play(*selected)

    def _export(self) -> None:
        if not self.project:
            return
        self.media_editor.commit_input()
        self._ensure_rebuilt()
        if not self.timeline.length:
            return
        self._pause()
        start, end = 0, self.timeline.length
        selected = self._range()
        if any(self._anchors) and not selected:
            QMessageBox.warning(self, "选段范围未完成", "请确认起点和终点，或清除选段后导出整集。")
            return
        if selected:
            choices = ["整集", f"当前选段　{timecode(selected[1] - selected[0], True)}"]
            choice, ok = QInputDialog.getItem(self, "导出范围", "选择本次导出内容", choices, 1, False)
            if not ok:
                return
            if choice == choices[1]:
                start, end = selected
        if self.timeline.media.conflicts_between(start, end):
            choice = QMessageBox.question(
                self, "导出范围内有重叠的背景",
                "重叠处将沿用预览中的取图顺序。要继续导出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        suffix = " - 选段" if (start or end < self.timeline.length) else ""
        path = self._save_location("导出回放视频", self.project.title + suffix, ".mp4", "MP4 视频 (*.mp4)")
        if not path:
            return
        dialog = ExportDialog(self)
        try:
            worker = ExportWorker(self.project, self.timeline, Path(self.media.name), start, end, path, dialog)
        except ReplayError as error:
            QMessageBox.warning(self, "无法导出", str(error))
            dialog.deleteLater()
            return
        dialog.watch(worker)
        if worker.cancelled:
            self._status("已取消导出")
        elif worker.error:
            QMessageBox.warning(self, "导出失败", worker.error)
        else:
            self.last_dir = path.parent
            self._status(f"MP4 已导出　{path}")
            QMessageBox.information(self, "导出完成", f"视频已保存。\n{path}")
        dialog.deleteLater()

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            suffix = Path(urls[0].toLocalFile()).suffix.lower()
            if suffix in (*LOG_SUFFIXES, PROJECT_SUFFIX):
                event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            event.acceptProposedAction()
            self.load_path(Path(urls[0].toLocalFile()))

    def closeEvent(self, event):
        if not self._confirm_replace():
            event.ignore()
            return
        self.update_timer.stop()
        self.filter_timer.stop()
        self.voice_editor.stop_workers()
        self._pause()
        self.audio.bind(None)
        if self.media:
            self.media.cleanup()
        event.accept()
