from __future__ import annotations

import gc
import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
import warnings
from array import array
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QContextMenuEvent, QFont, QFontDatabase, QImage, QRawFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMenu, QMessageBox, QPushButton

from replaystudio.appearance import configure_appearance
from replaystudio.exporting import ExportCancelled, ExportJob, find_ffmpeg
from replaystudio.model import DEFAULT_FONT, Scene, new_id, project_from_log
from replaystudio.render import build_timeline
from replaystudio.storage import (
    IMAGE_EXTENSIONS, MUSIC_EXTENSIONS, asset_path, import_asset, open_project, save_project,
)

SAMPLE = Path(__file__).resolve().parents[2] / "测试 跑团log.txt"
FIXTURE = """主持人(100001) 2026/11/10 20:00:02
旧门被推开了。
同一条消息的第二行。

侦察员(100002) 2026/11/10 20:00:02
#放下提灯🙂

主持人(100001) 2026/11/10 20:00:03

主持人(100001) 2026/11/11 20:00:02
当前先攻列表为:
1. 守卫: 15
2. 侦察员: 15
当前回合：守卫

主持人(100001) 2026/11/11 20:00:03
当前先攻列表为:
- 没有找到任何单位

主持人(100001) 2026/11/11 20:00:04
旅人离开了。
""".encode("utf-8")


def project():
    return project_from_log(FIXTURE, "回放测试.txt", "回放测试")


def tone(path: Path, hz: int):
    rate = 48_000
    samples = array("h", (round(12_000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(rate // 4)))
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, len(samples), "NONE", "not compressed"))
        output.writeframes(samples.tobytes())


def background(path: Path, color: str):
    canvas = QImage(64, 36, QImage.Format.Format_RGB32)
    canvas.fill(QColor(color))
    if not canvas.save(str(path)):
        raise RuntimeError(f"写测试图片失败 {path}")


def video_pixel(movie: Path, frame: int) -> tuple[int, int, int]:
    rgb = subprocess.check_output([
        find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(movie),
        "-vf", f"select=eq(n\\,{frame}),format=rgb24,crop=1:1:12:12", "-frames:v", "1",
        "-f", "rawvideo", "pipe:1",
    ])
    return tuple(rgb)


class ReplayAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_appearance(cls.app)

    @unittest.skipUnless(SAMPLE.is_file(), "需要本地完整跑团日志样本")
    def test_sample_keeps_every_nonempty_record_in_source_order(self):
        source = SAMPLE.read_bytes()
        result = project_from_log(source, SAMPLE.name, SAMPLE.stem)
        self.assertEqual(len(result.messages), 6160)
        self.assertEqual(len({message.stamp[:10] for message in result.messages}), 6)
        self.assertEqual(sum(not message.text for message in result.messages), 747)
        timeline = build_timeline(result)
        self.assertEqual(len(timeline.cue_frames), 5413)
        self.assertEqual(list(timeline.cue_frames), [
            f"m{message.number}" for message in result.messages if message.text
        ])
        self.assertTrue(any(len(page.lines) > 1 for page in timeline.pages))
        self.assertTrue(any(cue.text.startswith(".ra") and cue.id in timeline.cue_frames for cue in result.cues))
        self.assertEqual(result.source, source)

    def test_explicit_empty_initiative_clears_running_battle(self):
        result = project()
        result.insert(3, "battle_start")
        timeline = build_timeline(result)
        first = timeline.page_at(timeline.cue_frames["m4"])
        self.assertEqual([turn.name for turn in first.initiative.turns], ["守卫", "侦察员"])
        self.assertEqual(first.initiative.current, "守卫")
        self.assertIsNone(timeline.page_at(timeline.cue_frames["m6"]).initiative)

    def test_caption_font_renders_chinese_without_system_fonts(self):
        self.assertIn(DEFAULT_FONT, QFontDatabase.families())
        face = QRawFont.fromFont(QFont(DEFAULT_FONT, 12))
        self.assertTrue(face.isValid())
        self.assertTrue(all(face.supportsCharacter(c) for c in "跑团回放清晰可读"))

    def test_window_imports_and_edits_with_keyboard(self):
        self.assertIsNotNone(find_spec("replaystudio.window"), "需要可操作的 Windows 编辑器")
        from replaystudio.window import MainWindow

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "时间线.txt"
            path.write_bytes(FIXTURE)
            window = MainWindow()
            window.resize(910, 510)
            window.show()
            try:
                self.assertLessEqual(window.width(), 910)
                self.assertLessEqual(window.height(), 510)
                with patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "")):
                    window.import_action.trigger()
                self.app.processEvents()
                self.assertGreaterEqual(window.clock_label.width(), window.clock_label.sizeHint().width())
                self.assertEqual(window.proxy.rowCount(), 6)
                self.assertEqual(window.stage.page.cue_id, "m1")
                window.date_filter.setCurrentIndex(window.date_filter.findData("2026-11-11"))
                self.assertEqual(window.proxy.rowCount(), 3)
                window.date_filter.setCurrentIndex(0)
                index = window.proxy.index(1, 2)
                window.table.scrollTo(index)
                QTest.mouseClick(window.table.viewport(), Qt.MouseButton.LeftButton,
                                 pos=window.table.visualRect(index).center())
                self.assertEqual(window.inspector.text.toPlainText(), "#放下提灯🙂")
                window.inspector.text.setPlainText("#拿起提灯")
                QTest.qWait(250)
                self.assertEqual(window.stage.page.lines, ("#拿起提灯",))
            finally:
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
                    window.close()

    def test_editor_assigns_npc_and_exports_selected_frames_with_timeline_media(self):
        from replaystudio.window import MainWindow

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / "回放测试.txt"
            path.write_bytes(FIXTURE)
            portrait = directory / "立绘.png"
            backdrop = directory / "背景.png"
            music = directory / "配乐.wav"
            background(portrait, "#ff9041")
            background(backdrop, "#235885")
            tone(music, 330)
            editor = MainWindow()
            editor.resize(1460, 870)
            editor.show()
            self.addCleanup(editor.deleteLater)

            def click(caption):
                control = next(b for b in editor.findChildren(QPushButton) if b.text() == caption and b.isVisible())
                QTest.mouseClick(control, Qt.MouseButton.LeftButton)

            self.addCleanup(lambda: self._close_editor(editor))
            self.assertTrue(editor.load_path(path))
            editor.inspector.setCurrentIndex(1)
            with patch.object(QInputDialog, "getText", return_value=("向导", True)):
                click("添加 NPC")
            npc = editor.inspector.roles.currentData()
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(portrait), "")):
                click("选择立绘")
            self.assertTrue(editor.project.roles[npc].portrait)
            editor.inspector.setCurrentIndex(0)
            editor.inspector.assignee.setCurrentIndex(editor.inspector.assignee.findData(npc))
            click("分配角色")
            editor.inspector.duration.setValue(1)
            editor._select_ids({"m2"})
            editor.inspector.duration.setValue(1)
            editor.workspace_tabs.setCurrentIndex(1)
            with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(backdrop), str(music)], "")):
                click("导入素材")
            background_ref, music_ref = editor.project.media.library
            editor.media_editor.add_clip(background_ref, "background", editor.frame)
            editor.media_editor.add_clip(music_ref, "music", editor.frame)
            editor.workspace_tabs.setCurrentIndex(0)

            editor._select_ids({"m1"})
            self.assertEqual(editor.stage.page.role, npc)
            editor.inspector.setCurrentIndex(editor.inspector.template_tab_index)
            old_box = editor.project.template.portrait
            frame = editor.stage.frame_rect()
            position = (frame.topLeft() + QPointF(old_box.x + 30, old_box.y + 30) *
                        (frame.width() / 1920)).toPoint()
            QTest.mousePress(editor.stage, Qt.MouseButton.LeftButton, pos=position)
            QTest.mouseMove(editor.stage, position + QPoint(24, 10))
            QTest.mouseRelease(editor.stage, Qt.MouseButton.LeftButton, pos=position + QPoint(24, 10))
            self.assertGreater(editor.project.template.portrait.x, old_box.x)
            click("标记起点")
            with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as save_dialog, \
                    patch.object(QMessageBox, "warning"):
                editor.export_action.trigger()
                save_dialog.assert_not_called()

            editor._select_ids({"m2"})
            self.assertEqual(editor.stage.background, background_ref)
            self.assertEqual(editor.timeline.media.audio[0].asset, music_ref)
            click("标记终点")
            movie = directory / "界面导出.mp4"
            with patch.object(QInputDialog, "getItem", side_effect=lambda *a: (a[3][1], True)), \
                    patch.object(QFileDialog, "getSaveFileName", return_value=(str(movie), "")), \
                    patch.object(QMessageBox, "information"), patch.object(QMessageBox, "warning") as warning:
                editor.export_action.trigger()
                warning.assert_not_called()
            self.assertTrue(movie.is_file())
            self.assertGreater(video_pixel(movie, 30)[2], 100)

    def test_export_selection_ends_at_the_marked_frame_within_a_cue(self):
        from replaystudio.window import MainWindow

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "范围.txt"
            path.write_bytes(FIXTURE)
            editor = MainWindow()
            editor.show()
            self.addCleanup(editor.deleteLater)
            self.addCleanup(lambda: self._close_editor(editor))
            self.assertTrue(editor.load_path(path))
            editor.inspector.duration.setValue(20)
            editor._ensure_rebuilt()
            editor.slider.setValue(30)
            QTest.mouseClick(editor.start_button, Qt.MouseButton.LeftButton)
            editor.slider.setValue(60)
            QTest.mouseClick(editor.end_button, Qt.MouseButton.LeftButton)
            self.assertEqual(editor._range(), (30, 61))

    @staticmethod
    def _close_editor(editor):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
            editor.close()

    def test_timeline_delete_key_removes_filtered_record_and_saves(self):
        from replaystudio.window import MainWindow

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / "时间线.txt"
            path.write_bytes(FIXTURE)
            editor = MainWindow()
            editor.show()
            self.addCleanup(editor.deleteLater)
            self.addCleanup(self._close_editor, editor)
            self.assertTrue(editor.load_path(path))
            editor.date_filter.setCurrentIndex(editor.date_filter.findData("2026-11-11"))
            editor.table.selectRow(1)

            editor.inspector.text.setFocus()
            self.app.processEvents()
            QTest.keyClick(editor.inspector.text, Qt.Key.Key_Delete)
            self.assertEqual([cue.id for cue in editor.project.cues], ["m1", "m2", "m3", "m4", "m5", "m6"])

            editor.table.setFocus()
            self.app.processEvents()
            QTest.keyClick(editor.table, Qt.Key.Key_Delete)
            self.assertEqual([cue.id for cue in editor.project.cues], ["m1", "m2", "m3", "m4", "m6"])
            self.assertEqual(editor.stage.page.cue_id, "m6")
            output = directory / "删除后.replay"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(output), "")):
                editor.save_action.trigger()
            saved = open_project(output, directory)
            self.assertNotIn("m5", {cue.id for cue in saved.cues})
            self.assertEqual(saved.source, FIXTURE)
            self.assertEqual(path.read_bytes(), FIXTURE)

    def test_timeline_context_menu_hides_and_deletes_the_clicked_record(self):
        from replaystudio.window import MainWindow

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "时间线.txt"
            path.write_bytes(FIXTURE)
            editor = MainWindow()
            editor.show()
            self.addCleanup(editor.deleteLater)
            self.addCleanup(self._close_editor, editor)
            self.assertTrue(editor.load_path(path))
            view = editor.table.viewport()

            def open_row_menu():
                index = editor.proxy.index(1, 2)
                QTest.mouseClick(view, Qt.MouseButton.RightButton, pos=editor.table.visualRect(index).center())
                if not isinstance(QApplication.activePopupWidget(), QMenu):
                    position = editor.table.visualRect(index).center()
                    self.app.sendEvent(view, QContextMenuEvent(
                        QContextMenuEvent.Reason.Mouse, position, view.mapToGlobal(position),
                    ))
                menu = QApplication.activePopupWidget()
                self.assertIsInstance(menu, QMenu)
                return menu

            menu = open_row_menu()
            try:
                actions = {action.text().split("\t", 1)[0]: action for action in menu.actions()}
                self.assertTrue({"删除记录", "隐藏", "恢复", "上移", "下移",
                                 "补写台词", "插入过场", "按光标拆分", "合并所选"}.issubset(actions))
                self.assertTrue(actions["隐藏"].isEnabled())
                self.assertFalse(actions["恢复"].isEnabled())
                actions["隐藏"].trigger()
                self.assertTrue(editor.project.cues[1].hidden)
                self.assertFalse(editor.project.cues[0].hidden)
            finally:
                menu.close()

            menu = open_row_menu()
            try:
                delete = next(action for action in menu.actions() if action.text().startswith("删除记录"))
                delete.trigger()
                self.assertEqual([cue.id for cue in editor.project.cues], ["m1", "m3", "m4", "m5", "m6"])
            finally:
                menu.close()

    def test_portable_package_reopens_with_assets_and_hidden_messages(self):
        with tempfile.TemporaryDirectory() as author, tempfile.TemporaryDirectory() as recipient:
            original = Path(author)
            transferred = Path(recipient)
            result = project()
            path = original / "背景.png"
            background(path, "#407888")
            item = import_asset(result, path, original, IMAGE_EXTENSIONS)
            result.scenes[result.default_scene].background = item.id
            result.cues[1].hidden = True
            result.cues[0].text = "作者改写后的台词"
            parts = result.split(0, 4)
            result.move({parts.id}, 1)
            result.template.body_size = 60
            result.template.name = "我的模板"
            packed = original / "交给团友.replay"
            save_project(result, packed, original)
            copied = transferred / packed.name
            shutil.copyfile(packed, copied)
            path.unlink()
            asset_path(item, original).unlink()

            reopened = open_project(copied, transferred)
            self.assertEqual(reopened.source, FIXTURE)
            self.assertEqual(reopened.template.body_size, 60)
            self.assertEqual(reopened.template.name, "我的模板")
            self.assertFalse(QImage(str(asset_path(reopened.assets[item.id], transferred))).isNull())
            cue = next(cue for cue in reopened.cues if cue.id == "m2")
            self.assertNotIn("m2", build_timeline(reopened).cue_frames)
            cue.hidden = False
            self.assertIn("m2", build_timeline(reopened).cue_frames)
            self.assertEqual(reopened.cues[2].text, "后的台词")

    def test_merged_manual_duration_remains_openable(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            result = project()
            result.cues[0].duration = result.cues[1].duration = 1801
            role = result.cues[0].role
            result.merge([0, 1], role)
            package = directory / "长台词.replay"
            save_project(result, package, directory)
            reopened = open_project(package, directory)
            self.assertEqual(reopened.cues[0].duration, 3602)

    def test_mp4_range_has_exact_frame_count_and_scene_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            edited = project()
            edited.cues = edited.cues[:2]
            for cue in edited.cues:
                cue.duration = 1.50
            for index, (paint, hz) in enumerate((("#722145", 220), ("#21567A", 660))):
                image_path = directory / f"背景{index}.png"
                music_path = directory / f"配乐{index}.wav"
                background(image_path, paint)
                tone(music_path, hz)
                scene = edited.scenes[edited.default_scene] if not index else Scene(new_id(), "码头")
                scene.background = import_asset(edited, image_path, directory, IMAGE_EXTENSIONS).id
                scene.music = import_asset(edited, music_path, directory, MUSIC_EXTENSIONS).id
                if index:
                    edited.scenes[scene.id] = scene
                    edited.insert(1, "scene", scene=scene.id)

            timeline = build_timeline(edited)
            output = directory / "测试跨场景.mp4"
            job = ExportJob(edited, timeline, directory, 15, 77, output, lambda *_: None)
            job.run()
            stream_info = subprocess.check_output([
                "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)
            ])
            video, audio = json.loads(stream_info)["streams"]
            self.assertEqual((video["codec_name"], video["width"], video["height"]), ("h264", 1920, 1080))
            self.assertEqual((video["r_frame_rate"], int(video["nb_frames"])), ("30/1", 62))
            self.assertEqual(audio["codec_name"], "aac")
            self.assertAlmostEqual(float(audio["duration"]), 62 / 30, delta=.03)
            self.assertGreater(video_pixel(output, 29)[0], 100)
            self.assertGreater(video_pixel(output, 30)[2], 100)

            sound = subprocess.check_output([
                find_ffmpeg(), "-v", "error", "-i", str(output), "-map", "0:a:0",
                "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1",
            ])
            samples = array("h")
            samples.frombytes(sound)

            def strength(start, hz):
                window = samples[start:start + 4800]
                return abs(sum(sample * math.sin(2 * math.pi * hz * index / 48000)
                               for index, sample in enumerate(window)))

            self.assertGreater(strength(12000, 220), 10 * strength(12000, 660))
            self.assertGreater(strength(60000, 660), 10 * strength(60000, 220))

            old_bytes = output.read_bytes()

            def cancel_on_encode(percent, _):
                if 63 < percent < 100:
                    interrupted.cancel()

            interrupted = ExportJob(edited, timeline, directory, 15, 77, output, cancel_on_encode)
            with warnings.catch_warnings(record=True) as emitted:
                warnings.simplefilter("always", ResourceWarning)
                with self.assertRaises(ExportCancelled):
                    interrupted.run()
                del interrupted
                gc.collect()
            self.assertFalse([item for item in emitted if "still running" in str(item.message)])
            self.assertEqual(output.read_bytes(), old_bytes)
            self.assertFalse(list(directory.glob(".replay-*.mp4")))
