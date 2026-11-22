import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog

from replaystudio.appearance import configure_appearance
from replaystudio.model import ReplayError, project_from_log
from replaystudio.window import MainWindow

PARAGRAPHS = """
<w:p><w:r><w:t>20:18:09 &lt;主持</w:t></w:r><w:r><w:t>人&gt;: 推开旧门。</w:t></w:r></w:p>
<w:p><w:r><w:t>门后传来脚步声。</w:t></w:r></w:p>
<w:p><w:r><w:t>20:19:00 &lt;守卫&gt;: #提起灯</w:t></w:r></w:p>
<w:p><w:r><w:t>20:20:00 &lt;主持人&gt;: .log off</w:t></w:r></w:p>
<w:p><w:r><w:t>19:03:08 &lt;主持人&gt;: 当前先攻列表为:</w:t><w:br/>
<w:t>1. 守卫: 15</w:t></w:r></w:p>
<w:p><w:r><w:t>2. 旅人: 12</w:t></w:r></w:p>
<w:p><w:r><w:t>19:04:00 &lt;守卫&gt;：&lt;旅人&gt;低声说话。</w:t></w:r></w:p>
"""


def docx_bytes(paragraphs=PARAGRAPHS):
    data = BytesIO()
    with ZipFile(data, "w") as docx:
        docx.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{paragraphs}</w:body></w:document>"
        ))
    return data.getvalue()


class DocxImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_appearance(cls.app)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.docx = Path(self.folder.name) / "跑团样本.DOCX"
        self.docx.write_bytes(docx_bytes())
        self.window = MainWindow()
        self.addCleanup(self.close_window)
        self.window.show()

    def close_window(self):
        self.window.dirty = False
        self.window.close()

    def test_choose_docx_keeps_messages_and_saves_a_reopenable_project(self):
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(self.docx), "")) as chooser:
            self.window.choose_log_button.click()
        self.assertIn("*.docx", chooser.call_args.args[3])
        self.assertEqual(self.window.proxy.rowCount(), 5)
        self.assertEqual(self.window.model.index(0, 0).data(), "20:18:09")
        project = self.window.project
        self.assertEqual(len(project.roles), 2)
        self.assertEqual(project.cues[0].role, project.cues[3].role)
        self.assertEqual(project.cues[0].text, "推开旧门。\n门后传来脚步声。")
        self.assertEqual(project.cues[4].text, "<旅人>低声说话。")
        self.assertEqual([(turn.name, turn.score) for turn in project.cues[3].initiative.turns],
                         [("守卫", "15"), ("旅人", "12")])
        self.assertEqual(self.window.date_filter.itemText(2), "第 2 场")
        self.window.date_filter.setCurrentIndex(2)
        self.assertEqual(self.window.proxy.rowCount(), 2)
        self.assertEqual(self.window.proxy.index(0, 0).data(), "19:03:08")

        saved = self.docx.with_suffix(".replay")
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(saved), "")):
            self.assertTrue(self.window._save())
        self.docx.unlink()
        self.assertTrue(self.window.load_path(saved))
        self.assertEqual(self.window.project.messages, project.messages)
        self.assertEqual(self.window.project.source, project.source)
        self.assertEqual(self.window.project.cues, project.cues)
        self.assertEqual(self.window.date_filter.itemText(2), "第 2 场")

    def test_drag_and_drop_imports_docx(self):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(self.docx))])
        args = (Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        entered = QDragEnterEvent(QPoint(20, 20), *args)
        QApplication.sendEvent(self.window, entered)
        self.assertTrue(entered.isAccepted())
        dropped = QDropEvent(QPointF(20, 20), *args)
        QApplication.sendEvent(self.window, dropped)
        self.assertTrue(dropped.isAccepted())
        self.assertEqual(self.window.proxy.rowCount(), 5)

    def test_invalid_or_unrecognized_docx_reports_the_import_problem(self):
        with self.assertRaisesRegex(ReplayError, "无法读取 DOCX"):
            project_from_log(b"broken zip", "损坏.docx", "损坏")
        unrelated = docx_bytes("<w:p><w:r><w:t>普通文档</w:t></w:r></w:p>")
        with self.assertRaisesRegex(ReplayError, "DOCX 中没有找到消息头"):
            project_from_log(unrelated, "其他.docx", "其他")


if __name__ == "__main__":
    unittest.main()
