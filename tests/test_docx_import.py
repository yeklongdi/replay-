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
from replaystudio.model import OLE2_MAGIC, ReplayError, project_from_log
from replaystudio.storage import open_project, save_project
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

    def test_docx_supports_dates_accounts_timeless_nospace_images_and_tables(self):
        # 1. 关闭「年月日不展示」+ 关闭「平台帐号隐藏」+ 无空格 + 图片占位
        full_docx = docx_bytes("""
<w:p><w:r><w:t>2026/10/28 20:18:09 &lt;海豹一号机(2589922907)&gt;: 新的故事开始了。</w:t></w:r></w:p>
<w:p><w:r><w:t>第二行内容。</w:t></w:r></w:p>
<w:p><w:r><w:t>2026/10/28 20:18:20&lt;改名海豹(2589922907)&gt;: [图:https://example.com/a.png]</w:t></w:r></w:p>
<w:p><w:r><w:t>2026/10/29 19:00:00 &lt;跨平台(QQ:12345)&gt;: &lt;守卫(受伤)&gt;继续前进。</w:t></w:r></w:p>
<w:p><w:r><w:t>2026/10/29 19:01:00 &lt;守卫(受伤)&gt;: 保持队形。</w:t></w:r></w:p>
""")
        path = Path(self.folder.name) / "全开关.docx"
        path.write_bytes(full_docx)
        self.assertTrue(self.window.load_path(path))
        self.assertEqual(self.window.date_filter.itemText(0), "不限日期")
        self.assertEqual(self.window.date_filter.itemText(1), "2026-10-28")
        self.assertEqual(self.window.date_filter.itemText(2), "2026-10-29")
        self.window.date_filter.setCurrentIndex(2)
        self.assertEqual(self.window.proxy.rowCount(), 2)
        project = self.window.project
        self.assertEqual(project.cues[0].role, project.cues[1].role)
        self.assertEqual(project.roles[project.cues[0].role].name, "海豹一号机")
        self.assertEqual(project.roles[project.cues[0].role].account, "2589922907")
        self.assertEqual(project.roles[project.cues[2].role].account, "QQ:12345")
        self.assertEqual(project.roles[project.cues[3].role].name, "守卫(受伤)")
        self.assertEqual(project.messages[3].account, "")
        self.assertEqual(project.cues[0].text, "新的故事开始了。\n第二行内容。")
        self.assertEqual(project.cues[1].text, "[图:https://example.com/a.png]")

        # 2. 开启「时间显示过滤」（无时间戳）+ .replay 往返
        timeless_docx = docx_bytes("""
<w:p><w:r><w:t>&lt;妮娅(1728983113)&gt;: 无时间开场。</w:t></w:r></w:p>
<w:p><w:r><w:t>续行描述。</w:t></w:r></w:p>
<w:p><w:r><w:t>&lt;妮娅&gt;: .log off</w:t></w:r></w:p>
<w:p><w:r><w:t>&lt;守卫&gt;: 第二场开始。</w:t></w:r></w:p>
""")
        timeless_path = Path(self.folder.name) / "无时间.docx"
        timeless_path.write_bytes(timeless_docx)
        self.window.dirty = False
        self.assertTrue(self.window.load_path(timeless_path))
        self.assertEqual(self.window.model.index(0, 0).data(), "—")
        self.assertEqual(self.window.model.index(0, 0).data(Qt.ItemDataRole.ToolTipRole), "无时间记录")
        self.assertEqual(self.window.date_filter.itemText(2), "第 2 场")
        saved = timeless_path.with_suffix(".replay")
        save_project(self.window.project, saved, Path(self.window.media.name))
        reopened = open_project(saved, Path(self.folder.name))
        self.assertEqual(reopened.messages, self.window.project.messages)

        # 3. Word 两栏对话表格 (<w:tbl>)
        table_docx = docx_bytes("""
<w:tbl>
  <w:tr>
    <w:tc><w:p><w:r><w:t>海豹一号机(2589922907)</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>第一句台词。</w:t></w:r></w:p><w:p><w:r><w:t>第二行台词。</w:t></w:r></w:p></w:tc>
  </w:tr>
  <w:tr>
    <w:tc><w:p><w:r><w:t>木落</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>收到。</w:t></w:r></w:p></w:tc>
  </w:tr>
</w:tbl>
""")
        table_project = project_from_log(table_docx, "对话表.docx", "对话表")
        self.assertEqual(len(table_project.messages), 2)
        self.assertEqual(table_project.messages[0].name, "海豹一号机")
        self.assertEqual(table_project.messages[0].account, "2589922907")
        self.assertEqual(table_project.messages[0].text, "第一句台词。\n第二行台词。")
        self.assertEqual(table_project.messages[1].name, "木落")

    def test_mhtml_doc_exports_and_drag_drop_and_ole2_rejection(self):
        # 1. 下载带图doc（MHTML + raw UTF-8 + quoted-printable + 图片 + 多行 <p>）
        mhtml_raw = (
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/related; boundary="----=_NextPart_Test"\r\n\r\n'
            "------=_NextPart_Test\r\n"
            'Content-Type: text/html; charset="utf-8"\r\n\r\n'
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>\n"
            '<span class="_time">2026/10/28 20:18:09</span>'
            '<span class="_nickname">&lt;妮娅(1728983113)&gt;:</span>'
            '<span class="_message">新的故事开始了！<p>记录已经开启</p></span>\n'
            '<span class="_time">2026/10/28 20:18:20</span>'
            '<span class="_nickname">&lt;木落&gt;:</span>'
            '<span class="_message"><img src="https://example.com/pic.jpg"></span>\n'
            "</body></html>\r\n"
            "------=_NextPart_Test--\r\n"
        ).encode("utf-8")
        doc_path = Path(self.folder.name) / "带图记录.doc"
        doc_path.write_bytes(mhtml_raw)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(doc_path), "")) as chooser:
            self.window.choose_log_button.click()
        self.assertIn("*.doc", chooser.call_args.args[3])
        self.assertEqual(len(self.window.project.messages), 2)
        self.assertEqual(self.window.project.messages[0].name, "妮娅")
        self.assertEqual(self.window.project.messages[0].account, "1728983113")
        self.assertEqual(self.window.project.messages[0].stamp, "2026-10-28 20:18:09")
        self.assertEqual(self.window.project.messages[0].text, "新的故事开始了！\n记录已经开启")
        self.assertEqual(self.window.project.messages[1].text, "[图:https://example.com/pic.jpg]")

        saved = doc_path.with_suffix(".replay")
        save_project(self.window.project, saved, Path(self.window.media.name))
        reopened = open_project(saved, Path(self.folder.name))
        self.assertEqual(reopened.messages, self.window.project.messages)

        # 2. 下载对话doc（quoted-printable MHTML + 两栏表格 + 拖放导入）
        import quopri
        dialogue_html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
            '<table style="border-collapse: collapse;"><tbody>'
            '<tr><td class="_nickname">海豹一号机(2589922907)</td>'
            '<td>旅途愉快！<p>继续前行</p></td></tr>'
            '<tr><td class="_nickname">守卫</td>'
            '<td><img src="https://example.com/map.png"></td></tr>'
            "</tbody></table></body></html>"
        ).encode("utf-8")
        mhtml_qp = (
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            + quopri.encodestring(dialogue_html)
        )
        dialogue_doc = Path(self.folder.name) / "对话记录.doc"
        dialogue_doc.write_bytes(mhtml_qp)
        self.window.dirty = False
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(dialogue_doc))])
        args = (Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        entered = QDragEnterEvent(QPoint(20, 20), *args)
        QApplication.sendEvent(self.window, entered)
        self.assertTrue(entered.isAccepted())
        dropped = QDropEvent(QPointF(20, 20), *args)
        QApplication.sendEvent(self.window, dropped)
        self.assertTrue(dropped.isAccepted())
        self.assertEqual(len(self.window.project.messages), 2)
        self.assertEqual(self.window.project.messages[0].name, "海豹一号机")
        self.assertEqual(self.window.project.messages[0].account, "2589922907")
        self.assertEqual(self.window.project.messages[0].text, "旅途愉快！\n继续前行")
        self.assertEqual(self.window.project.messages[1].text, "[图:https://example.com/map.png]")

        # 3. 旧版二进制 OLE2 .doc 拒绝并提示
        with self.assertRaisesRegex(ReplayError, "旧版二进制 .doc"):
            project_from_log(OLE2_MAGIC + b"\x00" * 64, "旧文档.doc", "旧文档")

    def test_text_code_exports_bbcode_echo_and_raw_variants(self):
        # 1. 论坛代码（颜色名 + 次行空格缩进 + 带年月日 + 带账号）
        bbs_single = (
            "[color=silver]2026/10/28 20:18:09[/color][color=crimson] <海豹一号机(2589922907)>第一行\n"
            "\u2002\u2002\u2002\u2002/\u2002\u2002/\u2002\u2002\u2002\u2002:\u2002\u2002:\u2002\u2002 "
            "<海豹一号机(2589922907)>第二行 [/color]\n"
            "[color=silver]2026/10/28 20:18:20[/color][color=orange] <木落>收到 [/color]"
        ).encode("utf-8")
        p_bbs = project_from_log(bbs_single, "论坛单行.txt", "论坛单行")
        self.assertEqual(len(p_bbs.messages), 2)
        self.assertEqual(p_bbs.messages[0].name, "海豹一号机")
        self.assertEqual(p_bbs.messages[0].account, "2589922907")
        self.assertEqual(p_bbs.messages[0].stamp, "2026-10-28 20:18:09")
        self.assertEqual(p_bbs.messages[0].text, "第一行\n第二行")
        self.assertEqual(p_bbs.messages[1].text, "收到")

        # 2. 论坛代码(无时间 + 十六进制)
        bbs_timeless = (
            "[color=#db2777] <妮娅>无时间单行\n"
            "\u2002 <妮娅>续行 [/color]\n"
            "[color=#ea580c] <木落>另一句 [/color]"
        ).encode("utf-8")
        p_bbs_timeless = project_from_log(bbs_timeless, "论坛无时间.txt", "论坛无时间")
        self.assertEqual(len(p_bbs_timeless.messages), 2)
        self.assertEqual(p_bbs_timeless.messages[0].stamp, "")
        self.assertEqual(p_bbs_timeless.messages[0].text, "无时间单行\n续行")

        # 3. 论坛代码(内容多行)
        bbs_multi = (
            "[color=silver]<妮娅(1728983113)>[/color][color=#db2777] 新的故事开始了\n"
            "记录已经开启 [/color]\n"
            "[color=silver]<木落>[/color][color=#ea580c] 测试开始 [/color]"
        ).encode("utf-8")
        p_multi = project_from_log(bbs_multi, "论坛多行.txt", "论坛多行")
        self.assertEqual(len(p_multi.messages), 2)
        self.assertEqual(p_multi.messages[0].name, "妮娅")
        self.assertEqual(p_multi.messages[0].account, "1728983113")
        self.assertEqual(p_multi.messages[0].text, "新的故事开始了\n记录已经开启")
        self.assertEqual(p_multi.messages[1].text, "测试开始")

        # 4. 回声工坊（含主持人 ,KP、语音标记 {*}、骰娘 #）
        echo_log = (
            "[妮娅,KP]:新的故事开始了{*}\n"
            "[妮娅,KP]:记录已经开启{*}\n"
            "# [骰娘]:检定成功\n"
            "[守卫(受伤)]:明白{*}"
        ).encode("utf-8")
        p_echo = project_from_log(echo_log, "回声工坊.txt", "回声工坊")
        self.assertEqual(len(p_echo.messages), 4)
        self.assertEqual((p_echo.messages[0].name, p_echo.messages[0].text), ("妮娅", "新的故事开始了"))
        self.assertEqual((p_echo.messages[2].name, p_echo.messages[2].text), ("骰娘", "检定成功"))
        self.assertEqual((p_echo.messages[3].name, p_echo.messages[3].text), ("守卫(受伤)", "明白"))

        # 5. 原始 TXT 扩展平台账号 + 横杠日期 + .replay 往返
        raw_extended = (
            "跨平台角色(DISCORD:987654) 2026-10-28 20:18:09\n"
            "你好。\n"
        ).encode("utf-8")
        p_raw = project_from_log(raw_extended, "原始扩展.txt", "原始扩展")
        self.assertEqual(p_raw.messages[0].account, "DISCORD:987654")
        self.assertEqual(p_raw.messages[0].stamp, "2026-10-28 20:18:09")
        saved = Path(self.folder.name) / "回声.replay"
        save_project(p_echo, saved, Path(self.folder.name))
        self.assertEqual(open_project(saved, Path(self.folder.name)).messages, p_echo.messages)


if __name__ == "__main__":
    unittest.main()
