from __future__ import annotations

from bisect import bisect_right
from dataclasses import replace
from pathlib import Path
from threading import Event

from PySide6.QtCore import QMimeData, QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QDrag, QFont, QImageReader
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QHeaderView, QInputDialog, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QProgressBar, QScrollArea, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from .audio import AudioCache, PreparationCancelled
from .inspector import blocked, label, number, row
from .media_timing import validate_background
from .media_view import ASSET_MIME, TrackView
from .model import FPS, MAX_MEDIA_FRAME, MediaBookmark, MediaClip, MediaTrack, Project, ReplayError, new_id
from .render import Timeline
from .storage import IMAGE_EXTENSIONS, MUSIC_EXTENSIONS, import_asset
from .widgets import button, panel, timecode


class MediaTask(QThread):
    progress = Signal(str)

    def __init__(self, work, parent):
        super().__init__(parent)
        self.work = work
        self.stop = Event()
        self.result = None
        self.error = None

    def run(self) -> None:
        try:
            self.result = self.work(self.stop, self.progress.emit)
        except Exception as error:
            self.error = error


class MediaPreparation(QDialog):
    def __init__(self, title: str, work, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(340)
        self.task = MediaTask(work, self)
        layout = QVBoxLayout(self)
        note = label(title)
        note.setWordWrap(True)
        self.task.progress.connect(note.setText)
        layout.addWidget(note)
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(progress)
        self.cancel = button("取消", "quiet")
        self.cancel.clicked.connect(self.reject)
        layout.addWidget(self.cancel, 0, Qt.AlignmentFlag.AlignRight)

    def run(self):
        self.task.finished.connect(self.accept)
        QTimer.singleShot(0, self.task.start)
        self.exec()
        self.task.wait()
        error, result = self.task.error, self.task.result
        self.deleteLater()
        if error is not None:
            raise error
        return result

    def reject(self) -> None:
        self.task.stop.set()
        self.cancel.setEnabled(False)


class AssetList(QListWidget):
    def startDrag(self, supported_actions) -> None:
        item = self.currentItem()
        if item is None:
            return
        mime = QMimeData()
        mime.setData(ASSET_MIME, item.data(Qt.ItemDataRole.UserRole).encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)
        drag.deleteLater()


def seconds(minimum: int = 0) -> QDoubleSpinBox:
    control = QDoubleSpinBox()
    control.setDecimals(3)
    control.setRange(minimum / FPS, MAX_MEDIA_FRAME / FPS)
    control.setSingleStep(1 / FPS)
    control.setSuffix(" 秒")
    control.setKeyboardTracking(False)
    return control


class MediaEditor(QObject):
    changed = Signal()
    seek = Signal(int)
    notice = Signal(str)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.project: Project | None = None
        self.cache: AudioCache | None = None
        self.plan: Timeline | None = None
        self.active = ""
        self.last_dir = Path.home()
        self.timeline = TrackView()
        self.timeline.seek.connect(self.seek)
        self.timeline.selection.connect(self.select)
        self.timeline.drop_asset.connect(self.add_clip)
        self.timeline.adjust_clip.connect(self.adjust_clip)
        self.timeline.delete_clip.connect(self.delete_clip)
        self.timeline.rename_bookmark.connect(self._rename_bookmark)
        self.timeline.delete_bookmark.connect(self._delete_bookmark)
        self.timeline.rename_track.connect(self._rename_track)
        self.timeline.duplicate_track.connect(self.duplicate_track)
        self.timeline.delete_track.connect(self.delete_track)
        self.timeline.reorder_track.connect(self.reorder_track)
        self.library_panel = self._library_panel()
        self.details_panel = self._details_panel()
        self.timeline_panel = self._timeline_panel()

    def _library_panel(self) -> QWidget:
        widget = panel()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(10, 9, 10, 10)
        title = row(label("素材库", "panelTitle"))
        title.addStretch()
        add = button("导入素材", "compact")
        add.clicked.connect(self._choose_assets)
        title.addWidget(add)
        layout.addLayout(title)
        self.library = AssetList()
        self.library.setDragEnabled(True)
        self.library.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.library.setAlternatingRowColors(True)
        layout.addWidget(self.library, 1)
        layout.addWidget(label("将素材拖到下方轨道", "meta"))
        return widget

    def _details_panel(self) -> QWidget:
        widget = panel()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(1, 1, 1, 2)
        self.details_tabs = QTabWidget()
        self.details_tabs.addTab(self._clip_settings(), "片段设置")
        self.details_tabs.addTab(self._bookmark_manager(), "书签管理")
        layout.addWidget(self.details_tabs)
        return widget

    def _clip_settings(self) -> QWidget:
        widget = QWidget()
        widget.setObjectName("inspectorBody")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(10, 9, 10, 10)
        self.clip_label = label("在轨道上选择片段", "meta")
        self.clip_label.setWordWrap(True)
        layout.addWidget(self.clip_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(scroll, 1)
        self.fields = QWidget()
        fields_layout = QVBoxLayout(self.fields)
        fields_layout.setContentsMargins(0, 2, 1, 0)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.start = seconds()
        self.length = seconds(1)
        self.source_in = seconds()
        self.source_in.setToolTip("从素材开头算起。循环时，超出素材原长的偏移会回绕。")
        self.volume = number(0, 100, " %")
        self.loop = QCheckBox("循环至片段结束")
        self.anchor_mode = QComboBox()
        self.anchor_mode.addItems(["固定时间", "跟随台词"])
        form.addRow("开始", self.start)
        form.addRow("时长", self.length)
        form.addRow("定位方式", self.anchor_mode)
        form.addRow("文件偏移", self.source_in)
        form.addRow("音量", self.volume)
        form.addRow("", self.loop)
        self.audio_fields = (self.source_in, self.volume, self.loop)
        self.audio_labels = tuple(form.labelForField(field) for field in self.audio_fields[:2])
        fields_layout.addLayout(form)
        self.anchor_hint = label("", "meta")
        self.anchor_hint.setWordWrap(True)
        fields_layout.addWidget(self.anchor_hint)
        self.rebind = button("绑定播放头处台词", "compact")
        self.rebind.clicked.connect(self._bind_at_playhead)
        fields_layout.addWidget(self.rebind)
        fields_layout.addStretch()
        scroll.setWidget(self.fields)
        self.start.valueChanged.connect(self._set_start)
        self.length.valueChanged.connect(self._set_length)
        self.source_in.valueChanged.connect(self._set_source)
        self.volume.valueChanged.connect(lambda volume: self._change_selected(volume=volume))
        self.loop.toggled.connect(lambda loop: self._change_selected(loop=loop))
        self.anchor_mode.currentIndexChanged.connect(self._set_anchor_mode)
        self.fields.hide()
        return widget

    def _bookmark_manager(self) -> QWidget:
        widget = QWidget()
        widget.setObjectName("inspectorBody")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(10, 9, 10, 10)
        self.bookmark_empty = label("尚无书签", "meta")
        layout.addWidget(self.bookmark_empty, 1, Qt.AlignmentFlag.AlignTop)
        self.bookmark_table = QTableWidget(0, 2)
        self.bookmark_table.setHorizontalHeaderLabels(["书签名称", "成片时间"])
        self.bookmark_table.verticalHeader().hide()
        self.bookmark_table.verticalHeader().setDefaultSectionSize(32)
        header = self.bookmark_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.bookmark_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.bookmark_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.bookmark_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.bookmark_table.setAlternatingRowColors(True)
        self.bookmark_table.setShowGrid(False)
        self.bookmark_table.cellClicked.connect(self._seek_bookmark)
        self.bookmark_table.cellActivated.connect(self._seek_bookmark)
        self.bookmark_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.bookmark_table.customContextMenuRequested.connect(self._show_bookmark_menu)
        layout.addWidget(self.bookmark_table, 1)
        self.bookmark_table.hide()
        return widget

    def _refresh_bookmarks(self) -> None:
        table = self.bookmark_table
        role = Qt.ItemDataRole.UserRole
        selected = table.item(table.currentRow(), 0)
        selected_id = selected.data(role) if selected is not None else None
        bookmarks = sorted(self.project.media.bookmarks, key=lambda mark: mark.frame)
        table.setRowCount(0)
        table.setRowCount(len(bookmarks))
        clock_font = QFont("Consolas")
        for index, bookmark in enumerate(bookmarks):
            name = QTableWidgetItem(bookmark.name)
            name.setData(role, bookmark.id)
            name.setToolTip(bookmark.name)
            position = QTableWidgetItem(timecode(bookmark.frame, True))
            position.setData(role, bookmark.frame)
            position.setFont(clock_font)
            position.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(index, 0, name)
            table.setItem(index, 1, position)
            if bookmark.id == selected_id:
                table.setCurrentCell(index, 0)
        self.bookmark_empty.setVisible(not bookmarks)
        table.setVisible(bool(bookmarks))

    def _seek_bookmark(self, row: int, _column: int) -> None:
        self.seek.emit(self.bookmark_table.item(row, 1).data(Qt.ItemDataRole.UserRole))

    def _show_bookmark_menu(self, position) -> None:
        index = self.bookmark_table.indexAt(position)
        if not index.isValid():
            return
        row = index.row()
        self.bookmark_table.selectRow(row)
        bookmark_id = self.bookmark_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        frame = self.bookmark_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self.bookmark_table)
        menu.addAction("跳到书签", lambda: self.seek.emit(frame))
        menu.addAction("书签改名", lambda: self._rename_bookmark(bookmark_id))
        menu.addAction("删除书签", lambda: self._delete_bookmark(bookmark_id))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.bookmark_table.viewport().mapToGlobal(position))

    def _timeline_panel(self) -> QWidget:
        widget = panel()
        widget.setMinimumHeight(175)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 5, 6, 3)
        layout.setSpacing(4)
        tracks = button("添加音轨", "compact")
        tracks.setProperty("hasMenu", True)
        menu = QMenu(tracks)
        menu.addAction("配乐轨", lambda: self.add_track("music"))
        menu.addAction("音效轨", lambda: self.add_track("sound"))
        tracks.setMenu(menu)
        bookmark = button("添加书签", "compact")
        bookmark.clicked.connect(self._add_bookmark)
        self.range_divider = QFrame()
        self.range_divider.setObjectName("toolbarDivider")
        self.range_divider.setFrameShape(QFrame.Shape.VLine)
        self.range_divider.hide()
        self.range_slot = QHBoxLayout()
        self.range_slot.setContentsMargins(0, 0, 0, 0)
        self.range_slot.setSpacing(6)
        smaller = button("−", "compact")
        smaller.setToolTip("缩小时间线　Ctrl + 滚轮")
        larger = button("+", "compact")
        larger.setToolTip("放大时间线　Ctrl + 滚轮")
        fit = button("适应整集", "compact")
        smaller.clicked.connect(lambda: self.timeline.zoom(1 / 1.5))
        larger.clicked.connect(lambda: self.timeline.zoom(1.5))
        fit.clicked.connect(self.timeline.fit)
        snap = QCheckBox("吸附")
        snap.setToolTip("在拖动和裁切时吸附到台词边界或播放头")
        snap.setChecked(True)
        snap.toggled.connect(lambda active: setattr(self.timeline, "snap", active))
        tools = row(tracks, bookmark, self.range_divider)
        tools.addLayout(self.range_slot)
        tools.addStretch()
        for control in (snap, smaller, larger, fit):
            tools.addWidget(control)
        layout.addLayout(tools)
        layout.addWidget(self.timeline, 1)
        return widget

    def bind(self, project: Project, cache: AudioCache) -> None:
        self.project = project
        self.cache = cache
        self.plan = None
        self.timeline.reset()
        self.select("")
        self.details_tabs.setCurrentIndex(0)
        self._refresh_library()
        self._refresh_bookmarks()

    def update_timeline(self, timeline: Timeline) -> None:
        self.plan = timeline
        self.project.media.clips[:] = timeline.media.clips.values()
        self.timeline.bind(self.project, timeline)
        self._refresh_details()

    def _refresh_library(self) -> None:
        self.library.clear()
        for ref in self.project.media.library:
            asset = self.project.assets[ref]
            prefix = "图片" if asset.extension in IMAGE_EXTENSIONS else "音频"
            item = QListWidgetItem(f"{prefix}　{asset.name}")
            item.setToolTip(asset.name)
            item.setData(Qt.ItemDataRole.UserRole, ref)
            self.library.addItem(item)

    def _choose_assets(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self.library_panel, "导入剪辑素材", str(self.last_dir),
            "图片和音频 (*.png *.jpg *.jpeg *.mp3 *.wav);;背景图片 (*.png *.jpg *.jpeg);;音频 (*.mp3 *.wav)",
        )
        if paths:
            self.last_dir = Path(paths[0]).parent
            self.import_assets([Path(path) for path in paths])

    def import_assets(self, paths: list[Path]) -> None:
        if self.project is None:
            return
        project, cache = self.project, self.cache
        known = set(project.media.library)

        def load(stop, progress):
            added, errors = [], []
            for path in paths:
                if stop.is_set():
                    break
                try:
                    progress(f"读取　{path.name}")
                    is_image = path.suffix.lower() in IMAGE_EXTENSIONS
                    if is_image and QImageReader(str(path)).read().isNull():
                        raise ReplayError("无法读取图片。")
                    extensions = IMAGE_EXTENSIONS if is_image else MUSIC_EXTENSIONS
                    asset = import_asset(project, path, cache.directory, extensions)
                    if not is_image:
                        cache.decoded(asset.id, stop=stop)
                    if asset.id not in known:
                        known.add(asset.id)
                        added.append(asset.id)
                except PreparationCancelled:
                    break
                except (OSError, ReplayError) as error:
                    errors.append(f"{path.name}　{error}")
            return added, errors, stop.is_set()

        added, errors, cancelled = MediaPreparation("导入剪辑素材", load, self.parent()).run()
        if added:
            project.media.library.extend(added)
            self._refresh_library()
            self.changed.emit()
        if errors:
            self.notice.emit("导入失败　" + "　·　".join(errors))
        elif cancelled:
            self.notice.emit(f"已结束导入，加入 {len(added)} 个素材")
        elif added:
            self.notice.emit(f"已导入 {len(added)} 个素材")

    def prepare_audio(self, references) -> None:
        cache = self.cache
        missing = [ref for ref in dict.fromkeys(references) if cache.cached(ref) is None]
        if not missing:
            return

        def decode(stop, progress):
            for ref in missing:
                progress(f"解码　{cache.project.assets[ref].name}")
                cache.decoded(ref, stop=stop)

        MediaPreparation("准备音频", decode, self.parent()).run()

    def _frame_length(self, reference: str) -> int:
        self.prepare_audio((reference,))
        return self.cache.frame_length(reference)

    def commit_input(self) -> None:
        for field in (self.start, self.length, self.source_in, self.volume):
            if field.hasFocus():
                field.interpretText()
                break

    def select(self, clip_id: str) -> None:
        self.active = clip_id
        self.timeline.select(clip_id)
        self._refresh_details()
        if clip_id:
            self.details_tabs.setCurrentIndex(0)

    def _selected(self) -> MediaClip | None:
        return self.plan.media.clips.get(self.active) if self.plan else None

    def _refresh_details(self) -> None:
        clip = self._selected()
        self.fields.setVisible(clip is not None)
        if clip is None:
            self.clip_label.setText("在轨道上选择片段")
            self.clip_label.setToolTip("")
            return
        asset = self.project.assets[clip.asset]
        self.clip_label.setText(asset.name)
        self.clip_label.setToolTip(asset.name)
        audio = asset.extension in MUSIC_EXTENSIONS
        for field in (*self.audio_fields, *self.audio_labels):
            field.setVisible(audio)
        with blocked(self.start, self.length, self.source_in, self.volume, self.loop, self.anchor_mode):
            self.start.setValue(clip.start / FPS)
            self.length.setValue(clip.length / FPS)
            self.source_in.setValue(clip.source_in / FPS)
            self.volume.setValue(clip.volume)
            self.loop.setChecked(clip.loop)
            self.anchor_mode.setCurrentIndex(int(bool(clip.anchor)))
        self.rebind.setVisible(bool(clip.anchor))
        if clip.id in self.plan.media.pending:
            hint = "待定位。恢复绑定的台词，或重新设定定位方式。"
        elif clip.anchor:
            cue = next(item for item in self.project.cues if item.id == clip.anchor)
            snippet = " ".join(cue.text.split())
            hint = f"绑定台词　{snippet[:56]}"
        else:
            hint = ""
        if clip.id in self.plan.media.conflicting_clips:
            hint = (hint + "\n" if hint else "") + "背景重叠。重叠处优先显示固定片段，其次按加入顺序。"
        self.anchor_hint.setText(hint)
        self.anchor_hint.setVisible(bool(hint))

    def _fits_track(self, asset: str, track: str) -> bool:
        expected = next(item.kind for item in self.project.media.tracks if item.id == track)
        is_image = self.project.assets[asset].extension in IMAGE_EXTENSIONS
        if is_image != (expected == "background"):
            self.notice.emit("图片请放在背景轨，音频请放在配乐轨或音效轨。")
            return False
        return True

    def add_clip(self, ref: str, track: str, frame: int) -> None:
        if ref not in self.project.media.library or not self._fits_track(ref, track):
            return
        try:
            image = self.project.assets[ref].extension in IMAGE_EXTENSIONS
            length = 5 * FPS if image else self._frame_length(ref)
            start = min(frame, MAX_MEDIA_FRAME - length)
            if start < 0:
                raise ReplayError("片段超出可用时间。")
            clip = MediaClip(new_id(), track, ref, start, length)
            validate_background(self.project.media, clip, self.plan.dialogue_starts, self.plan.media)
        except PreparationCancelled:
            return
        except (OSError, ReplayError) as error:
            self.notice.emit(str(error))
            return
        self.project.media.clips.append(clip)
        self.select(clip.id)
        self.changed.emit()

    def _position(self, clip: MediaClip, start: int) -> MediaClip:
        start = max(0, min(start, MAX_MEDIA_FRAME - clip.length))
        cue_start = self.plan.dialogue_starts.get(clip.anchor)
        offset = clip.offset if cue_start is None else start - cue_start
        return replace(clip, start=start, offset=offset)

    def _limit_audio(self, old: MediaClip, clip: MediaClip) -> MediaClip:
        if (clip.loop or self.project.assets[clip.asset].extension not in MUSIC_EXTENSIONS
                or (old.length, old.source_in, old.loop) == (clip.length, clip.source_in, clip.loop)):
            return clip
        available = self._frame_length(clip.asset)
        source = min(clip.source_in, available - 1)
        length = min(clip.length, available - source)
        return replace(clip, source_in=source, length=length)

    def _commit(self, old: MediaClip, new: MediaClip) -> None:
        try:
            new = self._limit_audio(old, new)
            new = replace(new, length=min(new.length, MAX_MEDIA_FRAME - new.start))
            moved = (new.start, new.length, new.track) != (old.start, old.length, old.track)
            located = old.id in self.plan.media.pending and new.anchor != old.anchor
            # Shortening within the current span can resolve part of an automatic overlap.
            shrunk = new.track == old.track and old.start <= new.start and new.end <= old.end
            if (moved and not shrunk) or located:
                validate_background(self.project.media, new, self.plan.dialogue_starts, self.plan.media)
        except PreparationCancelled:
            self._refresh_details()
            return
        except (OSError, ReplayError) as error:
            self.notice.emit(str(error))
            self._refresh_details()
            return
        if old == new:
            self._refresh_details()
            return
        clips = self.project.media.clips
        index = next(i for i, clip in enumerate(clips) if clip.id == old.id)
        clips[index] = new
        self.changed.emit()

    def adjust_clip(self, clip_id: str, mode: str, frame: int, track: str) -> None:
        clip = self.plan.media.clip(clip_id)
        if mode == "move":
            if self._fits_track(clip.asset, track):
                self._commit(clip, replace(self._position(clip, frame), track=track))
        elif mode == "left":
            audio = self.project.assets[clip.asset].extension in MUSIC_EXTENSIONS
            minimum = max(0, clip.start - clip.source_in) if audio else 0
            start = max(minimum, min(frame, clip.end - 1))
            delta = start - clip.start
            source = min(MAX_MEDIA_FRAME, clip.source_in + delta) if audio else 0
            resized = replace(clip, length=clip.length - delta, source_in=source)
            self._commit(clip, self._position(resized, start))
        elif mode == "right":
            self._commit(clip, replace(clip, length=max(1, frame - clip.start)))

    def _set_start(self) -> None:
        if clip := self._selected():
            self.adjust_clip(clip.id, "move", round(self.start.value() * FPS), clip.track)

    def _set_length(self) -> None:
        if clip := self._selected():
            self._change_selected(length=max(1, round(self.length.value() * FPS)))

    def _set_source(self) -> None:
        self._change_selected(source_in=round(self.source_in.value() * FPS))

    def _change_selected(self, **values) -> None:
        if clip := self._selected():
            self._commit(clip, replace(clip, **values))

    def _anchor_clip(self, clip: MediaClip, frame: int) -> None:
        starts = self.plan.dialogue_starts
        if not starts:
            self.notice.emit("请先保留一条可播放的台词或过场。")
            self._refresh_details()
            return
        index = max(0, bisect_right(list(starts.values()), frame) - 1)
        cue = list(starts)[index]
        self._commit(clip, replace(clip, anchor=cue, offset=clip.start - starts[cue]))

    def _set_anchor_mode(self, index: int) -> None:
        if clip := self._selected():
            if index == 0:
                self._commit(clip, replace(clip, anchor="", offset=0))
            elif not clip.anchor:
                self._anchor_clip(clip, clip.start)

    def _bind_at_playhead(self) -> None:
        if clip := self._selected():
            self._anchor_clip(clip, self.timeline.playhead)

    def delete_clip(self, clip_id: str) -> None:
        clips = self.project.media.clips
        clips[:] = [clip for clip in clips if clip.id != clip_id]
        self.select("")
        self.changed.emit()

    def add_track(self, kind: str) -> None:
        tracks = self.project.media.tracks
        name = "配乐" if kind == "music" else "音效"
        names = {track.name for track in tracks}
        index = 2
        while f"{name} {index}" in names:
            index += 1
        tracks.append(MediaTrack(new_id(), f"{name} {index}", kind))
        self.changed.emit()

    def _rename_track(self, track_id: str) -> None:
        track = next((item for item in self.project.media.tracks if item.id == track_id), None)
        if track is None:
            return
        name, ok = QInputDialog.getText(self.timeline, "轨道改名", "轨道名称", text=track.name)
        if ok and name.strip() and name.strip() != track.name:
            track.name = name.strip()
            self.changed.emit()

    def reorder_track(self, track_id: str, target_index: int) -> None:
        tracks = self.project.media.tracks
        old_index = next((i for i, item in enumerate(tracks) if item.id == track_id), -1)
        if old_index < 0:
            return
        target = max(0, min(len(tracks) - 1, target_index))
        if target == old_index:
            return
        track = tracks.pop(old_index)
        tracks.insert(target, track)
        self.timeline.select_track(track_id)
        self.changed.emit()

    def duplicate_track(self, track_id: str) -> None:
        tracks = self.project.media.tracks
        index = next((i for i, item in enumerate(tracks) if item.id == track_id), -1)
        if index < 0:
            return
        source = tracks[index]
        if source.kind == "background":
            self.notice.emit("背景轨不能复制。")
            return
        names = {item.name for item in tracks}
        base = f"{source.name} 副本"
        name = base
        counter = 2
        while name in names:
            name = f"{base} {counter}"
            counter += 1
        copied = MediaTrack(new_id(), name, source.kind)
        tracks.insert(index + 1, copied)
        duplicates = [
            replace(clip, id=new_id(), track=copied.id)
            for clip in self.project.media.clips if clip.track == source.id
        ]
        self.project.media.clips.extend(duplicates)
        self.timeline.select_track(copied.id)
        self.changed.emit()
        self.notice.emit(f"已复制轨道　{copied.name}")

    def delete_track(self, track_id: str) -> None:
        tracks = self.project.media.tracks
        index = next((i for i, item in enumerate(tracks) if item.id == track_id), -1)
        if index < 0:
            return
        target = tracks[index]
        if target.kind == "background":
            self.notice.emit("背景轨不能删除。")
            return
        track_clips = [clip for clip in self.project.media.clips if clip.track == track_id]
        if track_clips:
            choice = QMessageBox.question(
                self.timeline, "删除轨道",
                f"轨道「{target.name}」内有 {len(track_clips)} 个片段，确定一并删除吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        removed_ids = {clip.id for clip in track_clips}
        tracks.pop(index)
        self.project.media.clips[:] = [clip for clip in self.project.media.clips if clip.track != track_id]
        if self.active in removed_ids:
            self.select("")
        next_track = tracks[min(index, len(tracks) - 1)].id if tracks else ""
        self.timeline.select_track(next_track)
        self.changed.emit()
        self.notice.emit(f"已删除轨道　{target.name}")

    def _add_bookmark(self) -> None:
        frame = self.timeline.playhead
        name, ok = QInputDialog.getText(
            self.timeline, "添加书签", timecode(frame, True),
            text=f"书签 {len(self.project.media.bookmarks) + 1}",
        )
        if ok and name.strip():
            self.project.media.bookmarks.append(MediaBookmark(new_id(), name.strip(), frame))
            self._refresh_bookmarks()
            self.changed.emit()

    def _rename_bookmark(self, bookmark_id: str) -> None:
        bookmark = next(item for item in self.project.media.bookmarks if item.id == bookmark_id)
        name, ok = QInputDialog.getText(self.timeline, "书签改名", "书签名称", text=bookmark.name)
        if ok and name.strip() and name.strip() != bookmark.name:
            bookmark.name = name.strip()
            self._refresh_bookmarks()
            self.changed.emit()

    def _delete_bookmark(self, bookmark_id: str) -> None:
        bookmarks = self.project.media.bookmarks
        bookmarks[:] = [item for item in bookmarks if item.id != bookmark_id]
        self._refresh_bookmarks()
        self.changed.emit()
