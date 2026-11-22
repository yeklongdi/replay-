from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from array import array
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, QSettings, QTimer, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QFrame, QInputDialog, QMenu, QMessageBox, QPushButton

from replaystudio import model
from replaystudio.appearance import configure_appearance
from replaystudio.exporting import ExportJob, find_ffmpeg
from replaystudio.media_view import TRACK_TOP
from replaystudio.render import build_timeline
from replaystudio.storage import IMAGE_EXTENSIONS, MUSIC_EXTENSIONS, asset_path, import_asset, open_project, save_project
from replaystudio.window import MainWindow
from test_acceptance import background, tone, video_pixel

LOG = "主持人(1) 2026/11/22 20:00:00\n门打开了。\n\n向导(2) 2026/11/22 20:00:03\n往里走。\n".encode()


def make_project():
    project = model.project_from_log(LOG, "剪辑测试.txt", "剪辑测试")
    for cue in project.cues:
        cue.duration = 1
    return project


class EditingWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_appearance(cls.app)

    def test_media_timing_and_audio_overlap(self):
        self.assertTrue(hasattr(model, "MediaEdit"), "工程需要自己的剪辑轨道")
        project = make_project()
        project.media = model.MediaEdit()
        project.media.clips.extend((
            model.MediaClip("酒馆", "background", "image1", 25, 10),
            model.MediaClip("森林", "background", "image2", 35, 15, anchor="m2", offset=5),
            model.MediaClip("开门声", "sound", "a1", 40, 15),
            model.MediaClip("脚步声", "sound", "a2", 40, 45),
        ))
        first = build_timeline(project)
        self.assertEqual(first.media.background_at(34), "image1")
        self.assertEqual(first.media.background_at(35), "image2")
        self.assertEqual({clip.id for clip in first.media.audio}, {"开门声", "脚步声"})
        self.assertEqual(first.length, 85)
        self.assertIsNone(first.page_at(60))

        project.cues[0].duration = 2 / 3
        moved = build_timeline(project)
        self.assertEqual(moved.media.clip("森林").start, 25)
        self.assertEqual(moved.media.clip("森林").length, 15)
        self.assertTrue(moved.media.conflicts_between(25, 35))
        self.assertEqual(moved.media.background_at(30), "image1")
        self.assertEqual(moved.media.background_at(35), "image2")

        project.cues[1].hidden = True
        hidden = build_timeline(project)
        self.assertIn("森林", hidden.media.pending)
        self.assertEqual(hidden.media.background_at(35), "")
        project.cues[1].hidden = False
        self.assertNotIn("森林", build_timeline(project).media.pending)
        project.remove({1})
        self.assertIn("森林", build_timeline(project).media.pending)

    def test_audio_mixes_same_track_and_resumes_at_loop_offset(self):
        self.assertIsNotNone(find_spec("replaystudio.audio"), "预览和导出需要共用混音")
        from replaystudio.audio import AudioMixer, SAMPLES_PER_FRAME

        with tempfile.TemporaryDirectory() as directory:
            loop = Path(directory) / "bgm.pcm"
            hit = Path(directory) / "sound.pcm"
            loop.write_bytes(array("h", (100, 100, 200, 200, 300, 300)).tobytes())
            hit.write_bytes(array("h", (400, 400, 600, 600, 800, 800)).tobytes())
            clips = (
                model.MediaClip("loop", "sound", "a", 2, 9, source_in=1, loop=True),
                model.MediaClip("hit", "sound", "b", 2, 1, volume=50),
            )
            mixer = AudioMixer(clips, {"a": loop, "b": hit})
            # At 1600 + 1 source samples the first clip is on its third loop sample.
            result = array("h")
            result.frombytes(mixer.read(2 * SAMPLES_PER_FRAME + 1, 4))
            self.assertEqual(result.tolist(), [600, 600, 500, 500, 200, 200, 300, 300])

    def test_workspace_tabs_share_preview_and_playhead(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "剪辑测试.txt"
            path.write_bytes(LOG)
            window = MainWindow()
            window.show()
            self.addCleanup(window.deleteLater)
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
                try:
                    self.assertTrue(window.load_path(path))
                    self.assertTrue(hasattr(window, "workspace_tabs"), "窗口需要台词和剪辑两个页签")
                    window.slider.setValue(12)
                    window._mark(0)
                    window.slider.setValue(21)
                    window._mark(1)
                    stage = window.stage
                    window.workspace_tabs.setCurrentIndex(1)
                    self.assertIs(window.stage, stage)
                    self.assertEqual(window.frame, 21)
                    self.assertEqual(window._range(), (12, 22))
                    self.assertTrue(window.media_editor.timeline.isVisible())
                    self.assertNotIn("场景", [window.inspector.tabText(i) for i in range(window.inspector.count())])
                    window.resize(890, 480)
                    self.app.processEvents()
                    self.assertGreaterEqual(window.stage_stack.height(), window.stage.minimumHeight())
                    window.workspace_tabs.setCurrentIndex(0)
                    self.assertEqual(window.frame, 21)
                    self.assertFalse(window.media_editor.timeline.isVisible())
                    window._begin_play(12, 22)
                    window.workspace_tabs.setCurrentIndex(1)
                    self.assertTrue(window.playing)
                    self.assertEqual((window._play_first, window._play_end), (12, 22))
                finally:
                    window.close()

    def test_settings_menu_is_leftmost_and_gives_header_height_to_tracks(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            project_file = directory / "设置测试.replay"
            save_project(make_project(), project_file, directory)
            window = MainWindow()
            window.resize(1460, 900)
            window.show()
            self.addCleanup(window.deleteLater)
            self.addCleanup(self._close, window)
            self.assertIsNone(window.findChild(QFrame, "topbar"))
            menu = window.settings_button.menu()
            labels = [action.text() for action in menu.actions()]
            self.assertEqual(labels[:5], ["导入日志", "打开工程", "保存", "打包工程", "导出 MP4"])
            self.assertFalse(window.save_action.isEnabled())
            self.assertLess(window.settings_button.mapTo(window, QPoint()).x(),
                            window.workspace_tabs.mapTo(window, QPoint()).x())
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(project_file), "")):
                menu.actions()[1].trigger()
            self.assertIsNotNone(window.project)
            self.assertTrue(window.export_action.isEnabled())
            self.assertTrue(window.save_action.isEnabled())
            window.workspace_tabs.setCurrentIndex(1)
            self.app.processEvents()
            self.assertGreaterEqual(window.work_split.sizes()[1], 420)
            self.assertLessEqual(abs(window.stage.height() - 268), 16)
            self.assertTrue(window.start_button.isVisible())
            self.assertTrue(window.media_editor.timeline_panel.isAncestorOf(window.start_button))
            window.workspace_tabs.setCurrentIndex(0)
            self.app.processEvents()
            self.assertTrue(window.start_button.isVisible())
            self.assertFalse(window.media_editor.timeline_panel.isAncestorOf(window.start_button))

    def test_track_select_context_menu_duplicate_delete_and_drag_reorder(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            file = directory / "轨道管理测试.replay"
            wav = directory / "音效.wav"
            tone(wav, 440)
            project = make_project()
            project.media = model.MediaEdit()
            ref = import_asset(project, wav, directory, MUSIC_EXTENSIONS).id
            project.media.library.append(ref)
            project.media.clips.append(model.MediaClip("c1", "sound", ref, 10, 15, volume=70))
            save_project(project, file, directory)

            window = MainWindow()
            window.resize(1460, 900)
            window.show()
            self.addCleanup(window.deleteLater)
            self.addCleanup(self._close, window)
            self.assertTrue(window.load_path(file))
            window.workspace_tabs.setCurrentIndex(1)
            editor = window.media_editor
            view = editor.timeline
            self.app.processEvents()

            def row_head(index: int) -> QPoint:
                row = view.rows[index]
                return QPoint(40, round(view._track_y(row) + row.height / 2))

            # Left-click selects track header
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=row_head(2))
            self.assertEqual(view.selected_track, "sound")

            # Right-click background track: duplicate & delete disabled
            QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=row_head(0))
            bg_menu = QApplication.activePopupWidget()
            self.assertIsInstance(bg_menu, QMenu)
            try:
                actions = {a.text().split("\t", 1)[0]: a for a in bg_menu.actions() if a.text()}
                self.assertFalse(actions["复制轨道"].isEnabled())
                self.assertFalse(actions["删除轨道"].isEnabled())
                self.assertFalse(actions["上移"].isEnabled())
                self.assertTrue(actions["下移"].isEnabled())
            finally:
                bg_menu.close()

            # Right-click sound track: duplicate (with its clip), rename, move up
            QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=row_head(2))
            sound_menu = QApplication.activePopupWidget()
            self.assertIsInstance(sound_menu, QMenu)
            try:
                actions = {a.text().split("\t", 1)[0]: a for a in sound_menu.actions() if a.text()}
                actions["复制轨道"].trigger()
            finally:
                sound_menu.close()
            self.assertEqual(len(window.project.media.tracks), 4)
            dup = window.project.media.tracks[3]
            self.assertEqual(dup.name, "音效 副本")
            self.assertEqual(len([c for c in window.project.media.clips if c.track == dup.id]), 1)

            with patch.object(QInputDialog, "getText", return_value=("环境音", True)):
                QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=row_head(3))
                dup_menu = QApplication.activePopupWidget()
                try:
                    next(a for a in dup_menu.actions() if a.text().startswith("轨道改名")).trigger()
                finally:
                    dup_menu.close()
            self.assertEqual(window.project.media.tracks[3].name, "环境音")

            # Drag row 3 ("环境音") above row 1 ("配乐")
            start_pt = row_head(3)
            end_pt = QPoint(40, round(view._track_y(view.rows[1]) + 4))
            QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start_pt)
            QTest.mouseMove(view.viewport(), end_pt)
            QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end_pt)
            self.assertEqual([t.name for t in window.project.media.tracks], ["背景", "环境音", "配乐", "音效"])

            # Delete non-empty track via Delete key asks for confirmation; Yes deletes track & clip
            view.setFocus()
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                QTest.keyClick(view, Qt.Key.Key_Delete)
            self.assertEqual(len(window.project.media.tracks), 4)
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                QTest.keyClick(view, Qt.Key.Key_Delete)
            self.assertEqual([t.name for t in window.project.media.tracks], ["背景", "配乐", "音效"])
            self.assertEqual(len(window.project.media.clips), 1)

            # Save & reopen preserves track order
            QTest.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=row_head(2))
            move_menu = QApplication.activePopupWidget()
            try:
                next(a for a in move_menu.actions() if a.text() == "上移").trigger()
            finally:
                move_menu.close()
            self.assertEqual([t.name for t in window.project.media.tracks], ["背景", "音效", "配乐"])
            saved_path = directory / "轨道顺序.replay"
            self.assertTrue(window._write_package(saved_path))
            reopened = open_project(saved_path, directory)
            self.assertEqual([t.name for t in reopened.media.tracks], ["背景", "音效", "配乐"])

    def test_settings_palette_applies_to_chrome_and_restores_from_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs = QSettings(str(Path(folder) / "外观.ini"), QSettings.Format.IniFormat)
            try:
                with patch("replaystudio.appearance.theme_settings", return_value=prefs):
                    configure_appearance(self.app)
                    window = MainWindow()
                    self.addCleanup(window.deleteLater)
                    self.addCleanup(self._close, window)
                    colors = {action.text(): action for action in window.settings_button.menu().actions()}
                    colors["冷蓝灰"].trigger()
                    self.assertEqual(self.app.palette().window().color().name(), "#141a24")
                    self.assertTrue(colors["冷蓝灰"].isChecked())
                    self.assertFalse(colors["青绿"].isChecked())
                    colors["暖灰"].trigger()
                    self.assertEqual(self.app.palette().window().color().name(), "#201d1b")
                    configure_appearance(self.app)
                    restored = MainWindow()
                    self.addCleanup(restored.deleteLater)
                    self.addCleanup(self._close, restored)
                    self.assertEqual(self.app.palette().window().color().name(), "#201d1b")
                    shades = {action.text(): action for action in restored.settings_button.menu().actions()}
                    self.assertTrue(shades["暖灰"].isChecked())
            finally:
                configure_appearance(self.app)

    def test_bookmark_manager_lists_chronologically_and_seeks_outside_view(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            project = make_project()
            project.cues[0].duration = 60
            project.media = model.MediaEdit()
            project.media.bookmarks.extend((
                model.MediaBookmark("late", "城门内", 1812),
                model.MediaBookmark("early", "城门外", 15),
            ))
            file = directory / "书签测试.replay"
            save_project(project, file, directory)
            window = MainWindow()
            window.resize(1460, 900)
            window.show()
            self.addCleanup(window.deleteLater)
            self.addCleanup(self._close, window)
            self.assertTrue(window.load_path(file))
            window.workspace_tabs.setCurrentIndex(1)
            editor = window.media_editor
            editor.details_tabs.setCurrentIndex(1)
            self.app.processEvents()
            self.assertEqual(editor.details_tabs.tabText(1), "书签管理")
            table = editor.bookmark_table
            self.assertEqual([table.item(i, 0).text() for i in range(table.rowCount())], ["城门外", "城门内"])
            self.assertEqual(table.item(1, 1).text(), "00:01:00.400")
            point = table.visualItemRect(table.item(1, 1)).center()
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)
            self.assertEqual(window.frame, 1812)
            self.assertEqual(window.stage.page.cue_id, "m2")
            self.assertGreater(editor.timeline.horizontalScrollBar().value(), 0)
            self.assertEqual(editor.details_tabs.currentIndex(), 1)
            with patch.object(QInputDialog, "getText", return_value=("城内广场", True)):
                editor._rename_bookmark("late")
            self.assertEqual(table.item(1, 0).text(), "城内广场")
            editor._delete_bookmark("late")
            self.assertEqual(table.rowCount(), 1)

    def test_dialogue_blocks_have_real_visual_gaps_without_changing_timing(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            file = directory / "台词块测试.replay"
            save_project(make_project(), file, directory)
            window = MainWindow()
            window.resize(1460, 900)
            window.show()
            self.addCleanup(window.deleteLater)
            self.addCleanup(self._close, window)
            self.assertTrue(window.load_path(file))
            window.workspace_tabs.setCurrentIndex(1)
            view = window.media_editor.timeline
            self.app.processEvents()
            previous = window.timeline.length
            first, second = view._dialogue_rect(0), view._dialogue_rect(1)
            self.assertGreaterEqual(second.left() - first.right(), 5)
            image = view.viewport().grab().toImage()
            ratio = image.devicePixelRatio()
            inside = QPointF(first.center().x(), first.top() + 5)
            between = QPointF(view._x(30), first.top() + 5)
            inside_color = image.pixelColor(round(inside.x() * ratio), round(inside.y() * ratio))
            gap_color = image.pixelColor(round(between.x() * ratio), round(between.y() * ratio))
            self.assertNotEqual(inside_color, gap_color)
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=second.center().toPoint())
            self.assertEqual(window.frame, 30)
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(round(view._x(39)), 10))
            self.assertEqual(window.frame, 39)
            self.assertEqual(window.timeline.length, previous)

    def test_editing_workspace_drag_trim_bookmark_and_follow_dialogue(self):
        from replaystudio.media_view import ASSET_MIME

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "剪辑.replay"
            save_project(make_project(), source, directory)
            red, blue = directory / "酒馆.png", directory / "城门.jpg"
            background(red, "#842a39")
            background(blue, "#3455ad")
            window = MainWindow()
            window.resize(1460, 900)
            window.show()
            self.addCleanup(window.deleteLater)
            self.addCleanup(self._close, window)
            self.assertTrue(window.load_path(source))
            window.workspace_tabs.setCurrentIndex(1)
            editor = window.media_editor
            view = editor.timeline
            self.app.processEvents()

            with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(red), str(blue)], "")):
                self._click(window, "导入素材")
            self.assertEqual(editor.library.count(), 2)

            def drop(index, frame):
                data = QMimeData()
                ref = editor.library.item(index).data(Qt.ItemDataRole.UserRole)
                data.setData(ASSET_MIME, ref.encode("ascii"))
                point = QPoint(round(view._x(frame)), TRACK_TOP + 18)
                args = (Qt.DropAction.CopyAction, data, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                QApplication.sendEvent(view.viewport(), QDragEnterEvent(point, *args))
                QApplication.sendEvent(view.viewport(), QDropEvent(QPointF(point), *args))
                return ref

            def drag(start, stop):
                a = QPoint(round(view._x(start)), TRACK_TOP + 18)
                b = QPoint(round(view._x(stop)), TRACK_TOP + 18)
                QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=a)
                QTest.mouseMove(view.viewport(), b)
                QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=b)

            first = drop(0, 0)
            one, = window.project.media.clips
            self.assertEqual((one.start, one.length), (0, 150))
            drop(1, 40)
            self.assertEqual(len(window.project.media.clips), 1)
            self.assertIn("重叠", window.statusBar().currentMessage())
            drag(149, 29)
            self.assertEqual(window.project.media.clips[0].length, 30)
            second = drop(1, 30)
            two = window.project.media.clips[1]
            drag(54, 39)
            self.assertEqual(window.project.media.clips[1], two)
            self.assertIn("重叠", window.statusBar().currentMessage())
            self.assertEqual(window.timeline.media.background_at(29), first)
            self.assertEqual(window.timeline.media.background_at(30), second)

            QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                              pos=QPoint(round(view._x(53)), TRACK_TOP + 18))
            self.assertEqual(window.frame, 30)
            with patch.object(QInputDialog, "getText", return_value=("城门入口", True)):
                self._click(window, "添加书签")
            self.app.processEvents()
            label_rect, _, _ = view.bookmark_rects[0]
            window._seek(3)
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=label_rect.center().toPoint())
            self.assertEqual(window.frame, 30)
            with patch.object(QInputDialog, "getText", return_value=("城门外", True)):
                QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=label_rect.center().toPoint())
            self.assertEqual(window.project.media.bookmarks[0].name, "城门外")
            old_scale = view.scale
            self._click(window, "+")
            self.assertGreater(view.scale, old_scale)

            editor.select(two.id)
            editor.anchor_mode.setCurrentIndex(1)
            self.assertEqual(window.project.media.clips[1].anchor, "m2")
            window.project.cues[0].duration = 2 / 3
            window._rebuild()
            self.assertTrue(window.timeline.media.conflicts_between(20, 30))
            self.assertEqual(window.timeline.media.background_at(22), first)
            self.assertEqual(window.timeline.media.background_at(30), second)
            window.project.cues[1].hidden = True
            window._rebuild()
            self.assertIn(two.id, window.timeline.media.pending)
            self.assertIn("待定位", editor.anchor_hint.text())
            window.project.cues[1].hidden = False
            window._rebuild()
            self.assertNotIn(two.id, window.timeline.media.pending)
            window._seek(64)
            self.assertIsNone(window.stage.page)
            self.assertEqual(window.stage.background, second)
            window._mark(0)
            window._seek(75)
            window._mark(1)
            self.assertEqual(window._range(), (64, 76))

    def test_looping_audio_left_trim_keeps_the_exact_source_phase(self):
        from replaystudio.audio import AudioMixer, SAMPLES_PER_FRAME

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / "剪辑.replay"
            wav = directory / "缓存音效.wav"
            save_project(make_project(), path, directory)
            tone(wav, 331)
            window = MainWindow()
            self.addCleanup(window.deleteLater)
            self.addCleanup(self._close, window)
            self.assertTrue(window.load_path(path))
            editor = window.media_editor
            editor.import_assets([wav])
            ref, = window.project.media.library
            editor.add_clip(ref, "sound", 0)
            editor.loop.setChecked(True)
            editor.length.setValue(1)
            original, = window.timeline.media.audio
            self.assertEqual(original.length, 30)
            pcm = {ref: editor.cache.decoded(ref)}
            expected = AudioMixer((original,), pcm).read(12 * SAMPLES_PER_FRAME, 2048)
            editor.adjust_clip(original.id, "left", 12, "sound")
            trimmed, = window.timeline.media.audio
            self.assertEqual((trimmed.start, trimmed.length, trimmed.source_in), (12, 18, 12))
            self.assertEqual(AudioMixer((trimmed,), pcm).read(12 * SAMPLES_PER_FRAME, 2048), expected)

    def test_audio_import_can_cancel_while_decoding(self):
        from replaystudio.audio import AudioCache, PreparationCancelled
        from replaystudio.media_editor import MediaPreparation

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / "剪辑.replay"
            wav = directory / "长音频.wav"
            save_project(make_project(), path, directory)
            tone(wav, 331)
            window = MainWindow()
            window.show()
            self.addCleanup(window.deleteLater)
            self.addCleanup(self._close, window)
            self.assertTrue(window.load_path(path))
            called = []

            def decode(cache, _ref, *, stop):
                called.append(stop.wait(3))
                raise PreparationCancelled()

            def cancel():
                dialog = QApplication.activeModalWidget()
                if isinstance(dialog, MediaPreparation):
                    dialog.cancel.click()

            with patch.object(AudioCache, "decoded", autospec=True, side_effect=decode):
                QTimer.singleShot(60, cancel)
                window.media_editor.import_assets([wav])
            self.assertEqual(called, [True])
            self.assertEqual(window.project.media.library, [])
            self.assertFalse(window.dirty)

    def test_pending_clip_input_commits_on_ctrl_save_and_close(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / "剪辑.replay"
            image = directory / "保存测试.png"
            background(image, "#467d68")
            project = make_project()
            project.media = model.MediaEdit()
            ref = import_asset(project, image, directory, IMAGE_EXTENSIONS).id
            project.media.library.append(ref)
            project.media.clips.append(model.MediaClip("clip", "background", ref, 0, 150))
            save_project(project, path, directory)
            window = MainWindow()
            window.show()
            self.addCleanup(window.deleteLater)
            self.addCleanup(self._close, window)
            self.assertTrue(window.load_path(path))
            window.workspace_tabs.setCurrentIndex(1)
            editor = window.media_editor
            editor.select("clip")
            self.app.processEvents()

            def type_start(value):
                field = editor.start.lineEdit()
                field.setFocus()
                field.selectAll()
                QTest.keyClicks(field, value)
                return field

            field = type_start("2.000")
            self.assertEqual(window.project.media.clips[0].start, 0)
            QTest.keyClick(field, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
            self.app.processEvents()
            self.assertEqual(open_project(path, directory).media.clips[0].start, 60)
            self.assertFalse(window.dirty)
            type_start("3.000")
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel) as confirm:
                self.assertFalse(window.close())
                confirm.assert_called_once()
            self.assertEqual(window.project.media.clips[0].start, 90)
            self.assertTrue(window.dirty)

    def test_legacy_media_migrates_and_portable_package_retains_assets(self):
        with tempfile.TemporaryDirectory() as source_folder, tempfile.TemporaryDirectory() as new_folder:
            src, dest = Path(source_folder), Path(new_folder)
            project = make_project()
            image, sound, unused = src / "旧背景.png", src / "旧音乐.wav", src / "未上轨.jpg"
            background(image, "#902240")
            background(unused, "#456a31")
            tone(sound, 330)
            project.scenes[project.default_scene].name = "开场"
            scene = model.Scene(model.new_id(), "第二幕")
            project.scenes[scene.id] = scene
            project.insert(1, "scene", scene=scene.id)
            scene.background = import_asset(project, image, src, IMAGE_EXTENSIONS).id
            scene.music = import_asset(project, sound, src, MUSIC_EXTENSIONS).id
            scene.volume = 46
            old_file = src / "旧工程.replay"
            save_project(project, old_file, src)
            before = build_timeline(project)
            with zipfile.ZipFile(old_file) as bundle:
                self.assertEqual(json.loads(bundle.read("project.json"))["version"], 1)

            window = MainWindow()
            self.addCleanup(window.deleteLater)
            try:
                self.assertTrue(window.load_path(old_file))
                self.assertEqual(window.timeline.media.audio, before.media.audio)
                self.assertEqual(window.timeline.media.backgrounds, before.media.backgrounds)
                bookmarks = [(item.name, item.frame) for item in window.project.media.bookmarks]
                self.assertEqual(bookmarks, [("开场", 0), ("第二幕", 30)])
                bg, bgm = window.project.media.clips
                self.assertEqual((bg.start, bg.length, bg.anchor), (30, 30, ""))
                self.assertEqual((bgm.volume, bgm.loop), (46, True))
                window.media_editor.import_assets([unused])
                window.media_editor.add_track("sound")
                extra = window.project.media.tracks[-1]
                window.media_editor.add_clip(scene.music, extra.id, 36)
                window.media_editor.anchor_mode.setCurrentIndex(1)
                last = window.project.media.clips[-1]
                self.assertEqual(last.anchor, "m2")
                window._select_ids({"m2"})
                window._hide(True)
                self.assertIn(last.id, window.timeline.media.pending)
                self.assertTrue(window._write_package(src / "续编.replay"))
                moved = dest / "团友续编.replay"
                shutil.copyfile(src / "续编.replay", moved)
            finally:
                self._close(window)

            with zipfile.ZipFile(moved) as bundle:
                metadata = json.loads(bundle.read("project.json"))
                self.assertEqual(metadata["version"], 2)
                self.assertEqual(bundle.read("source.txt"), LOG)
            reopened = open_project(moved, dest)
            self.assertEqual(len(reopened.media.library), 3)
            self.assertEqual(reopened.media.tracks[-1].kind, "sound")
            for ref in reopened.media.library:
                self.assertTrue(asset_path(reopened.assets[ref], dest).is_file())
            reopened.cues[-1].hidden = False
            resumed = build_timeline(reopened)
            self.assertNotIn(last.id, resumed.media.pending)
            self.assertEqual(resumed.media.clip(last.id).start, 36)
            output = dest / "续编导出.mp4"
            ExportJob(reopened, resumed, dest, 33, 48, output, lambda *_: None).run()
            self.assertGreater(video_pixel(output, 0)[0], 100)

    def test_mp4_mix_preserves_background_cut_in_dialogue_and_media_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            project = make_project()
            project.media = model.MediaEdit()

            def asset(name, value, write, extensions):
                path = directory / name
                write(path, value)
                ref = import_asset(project, path, directory, extensions).id
                project.media.library.append(ref)
                return ref

            red = asset("酒馆.png", "#8d1f43", background, IMAGE_EXTENSIONS)
            blue = asset("城门.jpg", "#1f5699", background, IMAGE_EXTENSIONS)
            source_wav = directory / "mp3-input.wav"
            tone(source_wav, 220)

            def write_mp3(path, _value):
                subprocess.run([find_ffmpeg(), "-v", "error", "-i", str(source_wav), str(path)], check=True)

            low = asset("配乐.mp3", None, write_mp3, MUSIC_EXTENSIONS)
            high = asset("脚步.wav", 660, tone, MUSIC_EXTENSIONS)
            project.media.clips.extend((
                model.MediaClip("red", "background", red, 0, 25),
                model.MediaClip("blue", "background", blue, 25, 60),
                model.MediaClip("low", "music", low, 0, 90, volume=55, loop=True),
                model.MediaClip("high", "music", high, 18, 27, source_in=1, volume=45, loop=True),
            ))
            timeline = build_timeline(project)
            self.assertEqual(timeline.dialogue_end, 60)
            self.assertEqual(timeline.length, 90)
            self.assertIs(timeline.page_at(24), timeline.page_at(25))
            self.assertIsNone(timeline.page_at(60))
            self.assertEqual(timeline.media.background_at(60), blue)
            output = directory / "混音选段.mp4"
            ExportJob(project, timeline, directory, 13, 84, output, lambda *_: None).run()
            data = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)])
            video, audio = json.loads(data)["streams"]
            self.assertEqual((video["r_frame_rate"], int(video["nb_frames"])), ("30/1", 71))
            self.assertAlmostEqual(float(audio["duration"]), 71 / 30, delta=.03)
            self.assertGreater(video_pixel(output, 11)[0], 100)
            self.assertGreater(video_pixel(output, 12)[2], 100)
            self.assertGreater(video_pixel(output, 70)[2], 100)
            samples = array("h")
            samples.frombytes(subprocess.check_output([
                find_ffmpeg(), "-v", "error", "-i", str(output), "-map", "0:a:0",
                "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1",
            ]))

            def strength(frame, hz):
                start = (frame - 13) * 1600
                period = 2 * math.pi * hz / 48000
                block = samples[start:start + 4800]
                return math.hypot(
                    sum(value * math.cos(period * index) for index, value in enumerate(block)),
                    sum(value * math.sin(period * index) for index, value in enumerate(block)),
                )

            self.assertGreater(strength(22, 220), 1_000_000)
            self.assertGreater(strength(22, 660), 1_000_000)
            self.assertGreater(strength(60, 220), 10 * strength(60, 660))

    @staticmethod
    def _click(window, caption):
        control = next(item for item in window.findChildren(QPushButton) if item.isVisible() and item.text() == caption)
        QTest.mouseClick(control, Qt.MouseButton.LeftButton)

    @staticmethod
    def _close(window):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
            window.close()
