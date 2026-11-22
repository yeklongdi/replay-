from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QSpinBox, QVBoxLayout, QWidget,
)

from .inspector import blocked, label, row
from .model import ReplayError, new_id
from .voice import (
    PROTOCOL_LABELS, VoiceApiConfig, decode_bytes_to_pcm, load_api_configs,
    new_api_config, save_api_configs, test_api_connection,
)
from .widgets import button, panel


class _ApiTestWorker(QThread):
    succeeded = Signal(bytes, int)
    failed = Signal(str)

    def __init__(self, config: VoiceApiConfig, parent: QWidget):
        super().__init__(parent)
        self.config = config

    def run(self) -> None:
        try:
            data, ext, latency = test_api_connection(self.config)
            pcm = decode_bytes_to_pcm(data, ext)
            self.succeeded.emit(pcm, latency)
        except (OSError, ReplayError) as error:
            self.failed.emit(str(error))


class VoiceApiDialog(QDialog):
    def __init__(self, audio, parent: QWidget | None = None):
        super().__init__(parent)
        self.audio = audio
        self.setWindowTitle("语音接口与自定义 API 配置")
        self.setMinimumSize(690, 500)
        self.configs: list[VoiceApiConfig] = [replace(item) for item in load_api_configs()]
        self._active_id: str = self.configs[0].id if self.configs else "__edge__"
        self._tester: _ApiTestWorker | None = None
        self._build_ui()
        self._refresh_list()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        body = QHBoxLayout()
        body.setSpacing(10)

        # Left list panel
        left = panel()
        left.setFixedWidth(210)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(10, 9, 10, 10)
        left_layout.setSpacing(8)
        top_row = row(label("已配置接口", "sectionLabel"))
        top_row.addStretch()
        self.add_btn = button("新建", "compact")
        self.add_btn.setProperty("hasMenu", True)
        add_menu = QMenu(self.add_btn)
        add_menu.addAction("GPT-SoVITS (api_v2)", lambda: self._add_config("gptsovits"))
        add_menu.addAction("OpenAI 兼容接口", lambda: self._add_config("openai"))
        add_menu.addAction("通用 HTTP JSON 模板", lambda: self._add_config("custom"))
        self.add_btn.setMenu(add_menu)
        top_row.addWidget(self.add_btn)
        left_layout.addLayout(top_row)

        self.api_list = QListWidget()
        self.api_list.setAlternatingRowColors(True)
        self.api_list.currentItemChanged.connect(self._selection_changed)
        left_layout.addWidget(self.api_list, 1)

        self.dup_btn = button("复制", "compact")
        self.del_btn = button("删除", "compact")
        self.dup_btn.clicked.connect(self._duplicate_current)
        self.del_btn.clicked.connect(self._delete_current)
        left_layout.addLayout(row(self.dup_btn, self.del_btn))
        body.addWidget(left)

        # Right form panel
        right = panel()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 10, 12, 10)
        right_layout.setSpacing(8)

        self.builtin_note = label(
            "内置 Edge-TTS 使用微软公开在线神经语音服务，开箱即用、免费免配置，提供 19 种中文与方言人声。\n\n"
            "如需接入自己部署的 GPT-SoVITS、CosyVoice、Fish Speech 或商用 OpenAI 兼容语音服务，请点左侧「新建」添加自定义接口。",
            "muted",
        )
        self.builtin_note.setWordWrap(True)
        right_layout.addWidget(self.builtin_note)

        self.form_widget = QWidget()
        form_layout = QVBoxLayout(self.form_widget)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(8)
        self.name_edit = QLineEdit()
        self.name_edit.textEdited.connect(self._commit_fields)
        form.addRow("接口名称", self.name_edit)

        self.proto_combo = QComboBox()
        for key, title in PROTOCOL_LABELS.items():
            self.proto_combo.addItem(title, key)
        self.proto_combo.currentIndexChanged.connect(self._proto_changed)
        form.addRow("协议模板", self.proto_combo)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("例如 http://127.0.0.1:9880/tts")
        self.url_edit.textEdited.connect(self._commit_fields)
        form.addRow("接口地址", self.url_edit)

        key_row = QHBoxLayout()
        key_row.setSpacing(6)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("选填，本地服务可留空")
        self.key_edit.textEdited.connect(self._commit_fields)
        self.show_key_btn = button("显示", "compact")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.toggled.connect(self._toggle_key_visibility)
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.show_key_btn)
        form.addRow("API Key", key_row)

        self.extra_label = label("模型 / 语种")
        self.extra_edit = QLineEdit()
        self.extra_edit.textEdited.connect(self._commit_fields)
        form.addRow(self.extra_label, self.extra_edit)

        self.auth_header_label = label("认证头名称")
        self.auth_header_edit = QLineEdit()
        self.auth_header_edit.setPlaceholderText("Authorization")
        self.auth_header_edit.textEdited.connect(self._commit_fields)
        form.addRow(self.auth_header_label, self.auth_header_edit)

        # GPT-SoVITS fields
        self.ref_audio_label = label("默认参考音频")
        ref_row = QHBoxLayout()
        ref_row.setSpacing(6)
        self.ref_audio_edit = QLineEdit()
        self.ref_audio_edit.setPlaceholderText("各角色可在「声音」页单独覆盖")
        self.ref_audio_edit.textEdited.connect(self._commit_fields)
        self.ref_browse_btn = button("浏览", "compact")
        self.ref_browse_btn.clicked.connect(self._browse_ref_audio)
        ref_row.addWidget(self.ref_audio_edit, 1)
        ref_row.addWidget(self.ref_browse_btn)
        self.ref_audio_wrap = QWidget()
        self.ref_audio_wrap.setLayout(ref_row)
        ref_row.setContentsMargins(0, 0, 0, 0)
        form.addRow(self.ref_audio_label, self.ref_audio_wrap)

        self.ref_text_label = label("默认参考文本")
        self.ref_text_edit = QLineEdit()
        self.ref_text_edit.setPlaceholderText("参考音频对应的文字内容")
        self.ref_text_edit.textEdited.connect(self._commit_fields)
        form.addRow(self.ref_text_label, self.ref_text_edit)

        # OpenAI / custom voice list
        self.voices_label = label("可用音色列表")
        self.voices_edit = QLineEdit()
        self.voices_edit.setPlaceholderText("多个音色用逗号分隔，例如 alloy, nova, kp")
        self.voices_edit.textEdited.connect(self._commit_fields)
        form.addRow(self.voices_label, self.voices_edit)

        # Custom JSON template
        self.body_label = label("JSON 模板")
        self.body_edit = QPlainTextEdit()
        self.body_edit.setPlaceholderText('{"text": "{text}", "speaker": "{voice}", "speed": {speed}}')
        self.body_edit.setMaximumHeight(74)
        self.body_edit.textChanged.connect(self._commit_fields)
        form.addRow(self.body_label, self.body_edit)

        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(8)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 300)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.valueChanged.connect(self._commit_fields)
        self.format_combo = QComboBox()
        self.format_combo.addItem("自动识别（WAV / MP3 / OGG）", "auto")
        self.format_combo.addItem("WAV", "wav")
        self.format_combo.addItem("MP3", "mp3")
        self.format_combo.currentIndexChanged.connect(self._commit_fields)
        timeout_row.addWidget(self.timeout_spin)
        timeout_row.addWidget(label("返回格式", "meta"))
        timeout_row.addWidget(self.format_combo, 1)
        form.addRow("请求超时", timeout_row)

        form_layout.addLayout(form)
        note = label(
            "安全提示：API 地址与密钥仅保存在本机软件设置中，打包 .replay 工程发给团友时不会写入密钥。",
            "noteWell",
        )
        note.setWordWrap(True)
        form_layout.addWidget(note)
        form_layout.addStretch()
        right_layout.addWidget(self.form_widget, 1)
        body.addWidget(right, 1)
        outer.addLayout(body, 1)

        # Footer
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.test_btn = button("测试连接并试听")
        self.test_btn.clicked.connect(self._test_current)
        self.test_status = label("", "muted")
        cancel = button("取消", "quiet")
        cancel.clicked.connect(self.reject)
        save = button("保存配置", "accent")
        save.clicked.connect(self._save_and_accept)
        foot.addWidget(self.test_btn)
        foot.addWidget(self.test_status, 1)
        foot.addWidget(cancel)
        foot.addWidget(save)
        outer.addLayout(foot)

    def _current_config(self) -> VoiceApiConfig | None:
        return next((item for item in self.configs if item.id == self._active_id), None)

    def _refresh_list(self) -> None:
        with blocked(self.api_list):
            self.api_list.clear()
            builtin = QListWidgetItem("内置 Edge-TTS　（免费）")
            builtin.setData(Qt.ItemDataRole.UserRole, "__edge__")
            self.api_list.addItem(builtin)
            selected_row = 0
            for idx, cfg in enumerate(self.configs, start=1):
                tag = {"gptsovits": "本地", "openai": "API", "custom": "自定义"}.get(cfg.protocol, "API")
                item = QListWidgetItem(f"{cfg.name}　[{tag}]")
                item.setData(Qt.ItemDataRole.UserRole, cfg.id)
                self.api_list.addItem(item)
                if cfg.id == self._active_id:
                    selected_row = idx
            self.api_list.setCurrentRow(selected_row)
        self._load_active()

    def _selection_changed(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None:
            return
        self._active_id = current.data(Qt.ItemDataRole.UserRole)
        self._load_active()

    def _load_active(self) -> None:
        cfg = self._current_config()
        is_custom = cfg is not None
        self.builtin_note.setVisible(not is_custom)
        self.form_widget.setVisible(is_custom)
        self.dup_btn.setEnabled(is_custom)
        self.del_btn.setEnabled(is_custom)
        self.test_btn.setEnabled(is_custom)
        self.test_status.setText("")
        if not cfg:
            return
        with blocked(
            self.name_edit, self.proto_combo, self.url_edit, self.key_edit, self.extra_edit,
            self.auth_header_edit, self.ref_audio_edit, self.ref_text_edit,
            self.voices_edit, self.body_edit, self.timeout_spin, self.format_combo,
        ):
            self.name_edit.setText(cfg.name)
            self.proto_combo.setCurrentIndex(max(0, self.proto_combo.findData(cfg.protocol)))
            self.url_edit.setText(cfg.url)
            self.key_edit.setText(cfg.api_key)
            self.extra_edit.setText(cfg.model_or_lang)
            self.auth_header_edit.setText(cfg.auth_header)
            self.ref_audio_edit.setText(cfg.ref_audio)
            self.ref_text_edit.setText(cfg.ref_text)
            self.voices_edit.setText(cfg.voices)
            self.body_edit.setPlainText(cfg.body_template)
            self.timeout_spin.setValue(cfg.timeout)
            self.format_combo.setCurrentIndex(max(0, self.format_combo.findData(cfg.response_format)))
        self._update_protocol_fields(cfg.protocol)

    def _update_protocol_fields(self, protocol: str) -> None:
        is_gpt = protocol == "gptsovits"
        is_custom = protocol == "custom"
        self.extra_label.setText("文本语种" if is_gpt else "模型名 (model)")
        self.extra_label.setVisible(not is_custom)
        self.extra_edit.setVisible(not is_custom)
        self.auth_header_label.setVisible(is_custom)
        self.auth_header_edit.setVisible(is_custom)
        self.ref_audio_label.setVisible(is_gpt)
        self.ref_audio_wrap.setVisible(is_gpt)
        self.ref_text_label.setVisible(is_gpt)
        self.ref_text_edit.setVisible(is_gpt)
        self.voices_label.setVisible(not is_gpt)
        self.voices_edit.setVisible(not is_gpt)
        self.body_label.setVisible(is_custom)
        self.body_edit.setVisible(is_custom)

    def _proto_changed(self) -> None:
        cfg = self._current_config()
        if cfg is None:
            return
        proto = self.proto_combo.currentData()
        cfg.protocol = proto
        self._update_protocol_fields(proto)
        self._commit_fields()
        self._refresh_list()

    def _commit_fields(self) -> None:
        cfg = self._current_config()
        if cfg is None:
            return
        cfg.name = self.name_edit.text().strip() or "未命名接口"
        cfg.protocol = self.proto_combo.currentData()
        cfg.url = self.url_edit.text().strip()
        cfg.api_key = self.key_edit.text()
        cfg.model_or_lang = self.extra_edit.text().strip()
        cfg.auth_header = self.auth_header_edit.text().strip() or "Authorization"
        cfg.ref_audio = self.ref_audio_edit.text().strip()
        cfg.ref_text = self.ref_text_edit.text().strip()
        cfg.voices = self.voices_edit.text().strip()
        cfg.body_template = self.body_edit.toPlainText()
        cfg.timeout = self.timeout_spin.value()
        cfg.response_format = self.format_combo.currentData()
        if current := self.api_list.currentItem():
            tag = {"gptsovits": "本地", "openai": "API", "custom": "自定义"}.get(cfg.protocol, "API")
            current.setText(f"{cfg.name}　[{tag}]")

    def _add_config(self, protocol: str) -> None:
        cfg = new_api_config(protocol)
        self.configs.append(cfg)
        self._active_id = cfg.id
        self._refresh_list()
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _duplicate_current(self) -> None:
        cfg = self._current_config()
        if cfg is None:
            return
        copied = replace(cfg, id=new_id(), name=cfg.name + " 副本")
        self.configs.append(copied)
        self._active_id = copied.id
        self._refresh_list()

    def _delete_current(self) -> None:
        cfg = self._current_config()
        if cfg is None:
            return
        self.configs = [item for item in self.configs if item.id != cfg.id]
        self._active_id = self.configs[0].id if self.configs else "__edge__"
        self._refresh_list()

    def _toggle_key_visibility(self, checked: bool) -> None:
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        self.show_key_btn.setText("隐藏" if checked else "显示")

    def _browse_ref_audio(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "选择参考音频", "", "音频 (*.wav *.mp3 *.ogg)")
        if selected:
            self.ref_audio_edit.setText(selected)
            self._commit_fields()

    def _test_current(self) -> None:
        cfg = self._current_config()
        if cfg is None:
            return
        self._commit_fields()
        if self._tester is not None and self._tester.isRunning():
            return
        self.test_btn.setEnabled(False)
        self.test_status.setText("正在请求接口试听…")
        self._tester = _ApiTestWorker(replace(cfg), self)
        self._tester.succeeded.connect(self._test_succeeded)
        self._tester.failed.connect(self._test_failed)
        self._tester.finished.connect(lambda: self.test_btn.setEnabled(self._current_config() is not None))
        self._tester.start()

    def _test_succeeded(self, pcm: bytes, latency: int) -> None:
        self.test_status.setText(f"连接成功 · 返回音频正常 ({latency} ms)")
        if self.audio is not None:
            self.audio.play_pcm(pcm)

    def _test_failed(self, message: str) -> None:
        self.test_status.setText("连接失败")
        QMessageBox.warning(self, "接口测试失败", message)

    def _save_and_accept(self) -> None:
        self._commit_fields()
        save_api_configs(self.configs)
        self.accept()

    def closeEvent(self, event) -> None:
        if self._tester is not None and self._tester.isRunning():
            self._tester.wait(1000)
        super().closeEvent(event)
