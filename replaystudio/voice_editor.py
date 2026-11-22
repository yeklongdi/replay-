from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QScrollArea,
    QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .audio import AudioCache, PreviewAudio
from .inspector import blocked, label, row
from .model import Cue, CueVoice, Project, ReplayError, RoleVoice
from .storage import VOICE_EXTENSIONS, import_asset
from .voice import (
    EDGE_VOICES, UT_PACES, UT_PRESETS, UT_PRESET_LABELS, VoiceApiConfig,
    api_config_map, decode_bytes_to_pcm, effective_cue_mode, ensure_role_voice,
    load_api_configs, prepare_spoken_segments, render_ut_pcm, role_voice_for,
    synthesize_cue_audio, synthesize_segment_bytes, voice_fingerprint,
)
from .voice_api_dialog import VoiceApiDialog
from .widgets import button, panel

NARRATOR_ID = ""


class _VoiceAuditionWorker(QThread):
    ready = Signal(bytes)
    failed = Signal(str)

    def __init__(self, text: str, rv: RoleVoice, apis: dict[str, VoiceApiConfig], parent: QObject):
        super().__init__(parent)
        self.text = text
        self.rv = rv
        self.apis = apis

    def run(self) -> None:
        try:
            data, ext = synthesize_segment_bytes(self.text, self.rv, self.apis)
            self.ready.emit(decode_bytes_to_pcm(data, ext))
        except (OSError, ReplayError) as error:
            self.failed.emit(str(error))


class _VoiceBatchWorker(QThread):
    item_done = Signal(str, str, str, int)  # cue_id, asset_id, fingerprint, duration_frames
    progress = Signal(int, int, str)
    finished_summary = Signal(int, int, str)  # generated, skipped, error_text

    def __init__(
        self,
        project: Project,
        cues: list[Cue],
        directory: Path,
        cache: AudioCache,
        apis: dict[str, VoiceApiConfig],
        parent: QObject,
    ):
        super().__init__(parent)
        self.project = project
        self.cues = cues
        self.directory = directory
        self.cache = cache
        self.apis = apis
        self.stop = Event()

    def run(self) -> None:
        total = len(self.cues)
        generated = 0
        skipped = 0
        errors: list[str] = []
        for idx, cue in enumerate(self.cues, start=1):
            if self.stop.is_set():
                break
            snippet = " ".join(cue.text.split())[:22]
            self.progress.emit(idx - 1, total, f"合成 {idx}/{total}：{snippet}")
            try:
                cv = self.project.voice.cues.get(cue.id)
                rv = role_voice_for(self.project.voice, cue.role)
                segs = prepare_spoken_segments(cue.text, self.project.voice, cv)
                if not segs:
                    skipped += 1
                    continue
                fp = voice_fingerprint(segs, rv, self.project.voice.narrator, self.apis)
                if cv and cv.fingerprint == fp and cv.asset in self.project.assets and cv.duration_frames > 0:
                    skipped += 1
                    continue
                asset, fp, frames = synthesize_cue_audio(
                    self.project, cue, self.directory, self.cache, self.apis,
                )
                generated += 1
                self.item_done.emit(cue.id, asset.id, fp, frames)
            except (OSError, ReplayError) as error:
                logging.warning("Batch voice failed for cue %s (%r): %s", cue.id, snippet, error)
                errors.append(str(error))
                if len(errors) >= 3:
                    break
        self.progress.emit(total, total, "")
        self.finished_summary.emit(generated, skipped, errors[0] if errors else "")


class VoiceEditor(QObject):
    changed = Signal()
    seek = Signal(int)
    notice = Signal(str)

    def __init__(self, audio: PreviewAudio, parent: QWidget):
        super().__init__(parent)
        self.audio = audio
        self.project: Project | None = None
        self.cache: AudioCache | None = None
        self.timeline = None
        self.selected_role: str = NARRATOR_ID
        self.selected_cue_id: str = ""
        self.api_configs: list[VoiceApiConfig] = load_api_configs()
        self._audition_worker: _VoiceAuditionWorker | None = None
        self._batch_worker: _VoiceBatchWorker | None = None
        self.left_panel = self._build_left_panel()
        self.cues_panel = self._build_cues_panel()
        self.right_panel = self._build_right_panel()

    def _build_left_panel(self) -> QWidget:
        frame = panel()
        frame.setMinimumWidth(240)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(11, 10, 11, 10)
        layout.setSpacing(8)

        head = row(label("角色与旁白", "panelTitle"))
        head.addStretch()
        self.role_count = label("0 项", "meta")
        head.addWidget(self.role_count)
        layout.addLayout(head)

        self.role_table = QTableWidget(0, 2)
        self.role_table.setHorizontalHeaderLabels(["名称", "方式"])
        self.role_table.verticalHeader().hide()
        self.role_table.verticalHeader().setDefaultSectionSize(34)
        self.role_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.role_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.role_table.setColumnWidth(1, 56)
        self.role_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.role_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.role_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.role_table.setAlternatingRowColors(True)
        self.role_table.setShowGrid(False)
        self.role_table.currentCellChanged.connect(self._role_row_changed)
        layout.addWidget(self.role_table, 1)

        rules_head = row(label("朗读与混音", "sectionLabel"))
        rules_head.addStretch()
        self.api_btn_left = button("API 接口…", "compact")
        self.api_btn_left.clicked.connect(self.open_api_dialog)
        rules_head.addWidget(self.api_btn_left)
        layout.addLayout(rules_head)

        self.skip_dice_check = QCheckBox("跳过骰点指令 (.r / .sc)")
        self.skip_dice_check.toggled.connect(self._global_rule_changed)
        layout.addWidget(self.skip_dice_check)

        duck_row = QHBoxLayout()
        duck_row.setSpacing(6)
        self.duck_check = QCheckBox("说话时压低配乐")
        self.duck_check.toggled.connect(self._global_rule_changed)
        self.duck_spin = QSpinBox()
        self.duck_spin.setRange(0, 100)
        self.duck_spin.setSuffix(" %")
        self.duck_spin.setKeyboardTracking(False)
        self.duck_spin.valueChanged.connect(self._global_rule_changed)
        duck_row.addWidget(self.duck_check, 1)
        duck_row.addWidget(self.duck_spin)
        layout.addLayout(duck_row)

        self.auto_dur_check = QCheckBox("配音自动延长台词时长")
        self.auto_dur_check.toggled.connect(self._global_rule_changed)
        layout.addWidget(self.auto_dur_check)

        bracket_row = QHBoxLayout()
        bracket_row.setSpacing(6)
        bracket_row.addWidget(label("括号内容", "meta"))
        self.bracket_combo = QComboBox()
        self.bracket_combo.addItem("跳过不读", "skip")
        self.bracket_combo.addItem("全部朗读", "keep")
        self.bracket_combo.addItem("切给旁白", "narrator")
        self.bracket_combo.currentIndexChanged.connect(self._global_rule_changed)
        bracket_row.addWidget(self.bracket_combo, 1)
        layout.addLayout(bracket_row)

        self.batch_progress = QProgressBar()
        self.batch_progress.setRange(0, 100)
        self.batch_progress.setFormat("AI 已缓存 0 / 0")
        layout.addWidget(self.batch_progress)

        self.generate_all_btn = button("生成全部 AI 配音", "accent")
        self.generate_all_btn.clicked.connect(self._toggle_batch_all)
        layout.addWidget(self.generate_all_btn)
        return frame

    def _build_cues_panel(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(5)
        head = row(label("当前角色台词", "sectionLabel"))
        self.cues_title = head.itemAt(0).widget()
        head.addStretch()
        self.cues_count = label("0 条 · 点选试听", "meta")
        head.addWidget(self.cues_count)
        layout.addLayout(head)

        self.cue_table = QTableWidget(0, 2)
        self.cue_table.horizontalHeader().hide()
        self.cue_table.verticalHeader().hide()
        self.cue_table.verticalHeader().setDefaultSectionSize(30)
        self.cue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.cue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.cue_table.setColumnWidth(1, 68)
        self.cue_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.cue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cue_table.setAlternatingRowColors(True)
        self.cue_table.setShowGrid(False)
        self.cue_table.setMinimumHeight(110)
        self.cue_table.setMaximumHeight(185)
        self.cue_table.cellClicked.connect(self._cue_row_clicked)
        layout.addWidget(self.cue_table)
        widget.hide()
        return widget

    def _build_right_panel(self) -> QWidget:
        frame = panel()
        frame.setMinimumWidth(268)
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(1, 1, 1, 2)
        self.right_tabs = QTabWidget()
        outer.addWidget(self.right_tabs)

        # Tab 1: 角色声音
        role_content = QWidget()
        role_content.setObjectName("inspectorBody")
        rl = QVBoxLayout(role_content)
        rl.setContentsMargins(12, 11, 12, 12)
        rl.setSpacing(9)

        role_head = QHBoxLayout()
        self.cfg_role_name = label("（旁白）", "sectionLabel")
        self.cfg_role_origin = label("旁白与过场", "meta")
        role_head.addWidget(self.cfg_role_name)
        role_head.addStretch()
        role_head.addWidget(self.cfg_role_origin)
        rl.addLayout(role_head)

        rl.addWidget(label("发声方式", "fieldTitle"))
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(5)
        self.mode_buttons: dict[str, QWidget] = {}
        for key, text in (("mute", "静音"), ("ut", "UT 音效"), ("ai", "AI 人声")):
            btn = button(text, "mode")
            btn.setCheckable(True)
            self.mode_group.addButton(btn)
            btn.clicked.connect(lambda _c=False, mode=key: self._set_role_mode(mode))
            self.mode_buttons[key] = btn
            mode_row.addWidget(btn)
        rl.addLayout(mode_row)

        # Mute box
        self.mute_box = QWidget()
        mute_layout = QVBoxLayout(self.mute_box)
        mute_layout.setContentsMargins(0, 4, 0, 0)
        mute_hint = label("该角色全部台词保持静音，适合骰娘、系统提示或纯字幕角色。", "muted")
        mute_hint.setWordWrap(True)
        mute_layout.addWidget(mute_hint)
        rl.addWidget(self.mute_box)

        # UT box
        self.ut_box = QWidget()
        ut_layout = QVBoxLayout(self.ut_box)
        ut_layout.setContentsMargins(0, 4, 0, 0)
        ut_layout.setSpacing(8)
        ut_layout.addWidget(label("对话音效预设（离线零负担）", "fieldTitle"))
        self.ut_preset_combo = QComboBox()
        for key, text in UT_PRESETS:
            self.ut_preset_combo.addItem(text, key)
        self.ut_preset_combo.currentIndexChanged.connect(self._ut_changed)
        ut_layout.addWidget(self.ut_preset_combo)

        self.ut_custom_row = QWidget()
        ut_custom_layout = QHBoxLayout(self.ut_custom_row)
        ut_custom_layout.setContentsMargins(0, 0, 0, 0)
        ut_custom_layout.setSpacing(6)
        self.ut_custom_label = label("未选择短音效", "fileLabel")
        self.ut_custom_btn = button("选择音频", "compact")
        self.ut_custom_btn.clicked.connect(self._choose_ut_asset)
        ut_custom_layout.addWidget(self.ut_custom_label, 1)
        ut_custom_layout.addWidget(self.ut_custom_btn)
        ut_layout.addWidget(self.ut_custom_row)

        self.ut_pitch_slider, self.ut_pitch_val = self._slider_row(ut_layout, "音高", -12, 12, self._ut_changed)
        self.ut_pace_slider, self.ut_pace_val = self._slider_row(ut_layout, "节奏", 0, 2, self._ut_changed)
        self.ut_vol_slider, self.ut_vol_val = self._slider_row(ut_layout, "音量", 0, 100, self._ut_changed)
        self.ut_audition_btn = button("试听音效", "accent")
        self.ut_audition_btn.clicked.connect(self._audition_ut)
        ut_layout.addWidget(self.ut_audition_btn)
        ut_layout.addWidget(label("完全离线、立即生效、导出极快", "meta"))
        rl.addWidget(self.ut_box)

        # AI box
        self.ai_box = QWidget()
        ai_layout = QVBoxLayout(self.ai_box)
        ai_layout.setContentsMargins(0, 4, 0, 0)
        ai_layout.setSpacing(8)

        prov_head = row(label("语音接口", "fieldTitle"))
        prov_head.addStretch()
        self.api_btn_right = button("配置 API…", "compact")
        self.api_btn_right.clicked.connect(self.open_api_dialog)
        prov_head.addWidget(self.api_btn_right)
        ai_layout.addLayout(prov_head)

        self.provider_combo = QComboBox()
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        ai_layout.addWidget(self.provider_combo)

        self.ai_voice_label = label("音色 / 说话人", "fieldTitle")
        ai_layout.addWidget(self.ai_voice_label)
        self.ai_voice_combo = QComboBox()
        self.ai_voice_combo.currentIndexChanged.connect(self._ai_changed)
        ai_layout.addWidget(self.ai_voice_combo)

        self.ai_voice_edit = QLineEdit()
        self.ai_voice_edit.setPlaceholderText("输入自定义音色名或留空使用默认")
        self.ai_voice_edit.editingFinished.connect(self._ai_changed)
        ai_layout.addWidget(self.ai_voice_edit)

        self.ref_override_box = QWidget()
        ref_layout = QVBoxLayout(self.ref_override_box)
        ref_layout.setContentsMargins(0, 0, 0, 0)
        ref_layout.setSpacing(6)
        ref_layout.addWidget(label("角色参考音频（留空沿用接口默认）", "fieldTitle"))
        ref_pick = QHBoxLayout()
        ref_pick.setSpacing(6)
        self.role_ref_audio = QLineEdit()
        self.role_ref_audio.editingFinished.connect(self._ai_changed)
        ref_btn = button("浏览", "compact")
        ref_btn.clicked.connect(self._browse_role_ref)
        ref_pick.addWidget(self.role_ref_audio, 1)
        ref_pick.addWidget(ref_btn)
        ref_layout.addLayout(ref_pick)
        self.role_ref_text = QLineEdit()
        self.role_ref_text.setPlaceholderText("参考音频对应文本（选填）")
        self.role_ref_text.editingFinished.connect(self._ai_changed)
        ref_layout.addWidget(self.role_ref_text)
        ai_layout.addWidget(self.ref_override_box)

        self.ai_rate_slider, self.ai_rate_val = self._slider_row(ai_layout, "语速", -40, 40, self._ai_changed)
        self.ai_pitch_slider, self.ai_pitch_val = self._slider_row(ai_layout, "音调", -30, 30, self._ai_changed)
        self.ai_vol_slider, self.ai_vol_val = self._slider_row(ai_layout, "音量", 0, 100, self._ai_changed)

        self.ai_audition_btn = button("试听一句")
        self.ai_audition_btn.clicked.connect(self._audition_ai)
        self.ai_gen_role_btn = button("生成本角色", "accent")
        self.ai_gen_role_btn.clicked.connect(self._generate_selected_role)
        ai_layout.addLayout(row(self.ai_audition_btn, self.ai_gen_role_btn))
        self.ai_role_status = label("已缓存 0 / 0 句", "meta")
        ai_layout.addWidget(self.ai_role_status)
        rl.addWidget(self.ai_box)
        rl.addStretch()

        scroll_role = QScrollArea()
        scroll_role.setWidgetResizable(True)
        scroll_role.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_role.setWidget(role_content)
        self.right_tabs.addTab(scroll_role, "角色声音")

        # Tab 2: 本句微调
        cue_content = QWidget()
        cue_content.setObjectName("inspectorBody")
        cl = QVBoxLayout(cue_content)
        cl.setContentsMargins(12, 11, 12, 12)
        cl.setSpacing(9)
        self.cue_head_label = label("选择一条台词进行单句微调", "sectionLabel")
        self.cue_origin_label = label("", "muted")
        self.cue_origin_label.setWordWrap(True)
        cl.addWidget(self.cue_head_label)
        cl.addWidget(self.cue_origin_label)

        cl.addWidget(label("本句发声方式", "fieldTitle"))
        self.cue_mode_combo = QComboBox()
        self.cue_mode_combo.addItem("跟随角色设置", "inherit")
        self.cue_mode_combo.addItem("本句静音（不配音）", "mute")
        self.cue_mode_combo.addItem("改用 UT 对话音效", "ut")
        self.cue_mode_combo.addItem("本地干声替换", "custom")
        self.cue_mode_combo.currentIndexChanged.connect(self._cue_mode_changed)
        cl.addWidget(self.cue_mode_combo)

        self.cue_custom_row = QWidget()
        cc_layout = QHBoxLayout(self.cue_custom_row)
        cc_layout.setContentsMargins(0, 0, 0, 0)
        cc_layout.setSpacing(6)
        self.cue_custom_label = label("未选择本地干声", "fileLabel")
        self.cue_custom_btn = button("选择音频", "compact")
        self.cue_custom_btn.clicked.connect(self._choose_cue_audio)
        cc_layout.addWidget(self.cue_custom_label, 1)
        cc_layout.addWidget(self.cue_custom_btn)
        cl.addWidget(self.cue_custom_row)

        cl.addWidget(label("口播文本（留空按正文朗读，不改字幕）", "fieldTitle"))
        self.spoken_edit = QPlainTextEdit()
        self.spoken_edit.setPlaceholderText("例如把 SAN 改成理智值，或修正多音字读音")
        self.spoken_edit.setMinimumHeight(90)
        self.spoken_edit.setMaximumHeight(140)
        self.spoken_edit.textChanged.connect(self._spoken_text_changed)
        cl.addWidget(self.spoken_edit)

        self.cue_audition_btn = button("试听本句")
        self.cue_audition_btn.clicked.connect(self._audition_current_cue)
        self.cue_regen_btn = button("重新生成", "accent")
        self.cue_regen_btn.clicked.connect(self._regen_current_cue)
        cl.addLayout(row(self.cue_audition_btn, self.cue_regen_btn))
        cl.addStretch()

        scroll_cue = QScrollArea()
        scroll_cue.setWidgetResizable(True)
        scroll_cue.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_cue.setWidget(cue_content)
        self.right_tabs.addTab(scroll_cue, "本句微调")
        return frame

    def _slider_row(self, parent_layout: QVBoxLayout, title: str, minimum: int, maximum: int, slot):
        line = QHBoxLayout()
        line.setSpacing(6)
        line.addWidget(label(title, "meta"))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.valueChanged.connect(slot)
        val = label("", "meta")
        val.setMinimumWidth(46)
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        line.addWidget(slider, 1)
        line.addWidget(val)
        parent_layout.addLayout(line)
        return slider, val

    def bind(self, project: Project, cache: AudioCache) -> None:
        self.stop_workers()
        self.project = project
        self.cache = cache
        self.api_configs = load_api_configs()
        self.selected_role = next(iter(project.roles), NARRATOR_ID)
        self.selected_cue_id = ""
        self._refresh_providers()
        self.refresh()

    def update_timeline(self, timeline) -> None:
        self.timeline = timeline

    def set_current_cue(self, cue_id: str) -> None:
        if not self.project or cue_id == self.selected_cue_id:
            return
        self.selected_cue_id = cue_id
        self._refresh_cue_detail()

    def refresh(self) -> None:
        if not self.project:
            return
        self._refresh_global_rules()
        self._refresh_role_list()
        self._refresh_role_detail()
        self._refresh_role_cues()
        self._refresh_cue_detail()
        self._refresh_batch_progress()

    def _refresh_providers(self) -> None:
        self.api_configs = load_api_configs()
        current = self.provider_combo.currentData()
        with blocked(self.provider_combo):
            self.provider_combo.clear()
            self.provider_combo.addItem("内置免费（Edge-TTS）", "edge")
            for cfg in self.api_configs:
                self.provider_combo.addItem(f"{cfg.name}（自定义）", cfg.id)
            idx = self.provider_combo.findData(current)
            self.provider_combo.setCurrentIndex(max(0, idx))

    def _refresh_global_rules(self) -> None:
        s = self.project.voice
        with blocked(self.skip_dice_check, self.duck_check, self.duck_spin, self.auto_dur_check, self.bracket_combo):
            self.skip_dice_check.setChecked(s.skip_dice)
            self.duck_check.setChecked(s.duck_music)
            self.duck_spin.setValue(s.duck_level)
            self.duck_spin.setEnabled(s.duck_music)
            self.auto_dur_check.setChecked(s.auto_duration)
            self.bracket_combo.setCurrentIndex(max(0, self.bracket_combo.findData(s.bracket_mode)))

    def _role_entries(self) -> list[tuple[str, str]]:
        entries = [(role.id, role.name) for role in self.project.roles.values()]
        entries.append((NARRATOR_ID, "（旁白）"))
        return entries

    def _role_cue_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cue in self.project.cues:
            if cue.is_text and not cue.hidden and cue.text.strip():
                counts[cue.role] = counts.get(cue.role, 0) + 1
        return counts

    def _refresh_role_list(self) -> None:
        entries = self._role_entries()
        if all(rid != self.selected_role for rid, _ in entries):
            self.selected_role = entries[0][0] if entries else NARRATOR_ID
        counts = self._role_cue_counts()
        apis = api_config_map(self.api_configs)
        self.role_count.setText(f"{len(entries)} 项")
        with blocked(self.role_table):
            self.role_table.setRowCount(len(entries))
            target_row = 0
            for idx, (rid, name) in enumerate(entries):
                rv = role_voice_for(self.project.voice, rid)
                count = counts.get(rid, 0)
                if rv.mode == "ut":
                    badge = "UT"
                    detail = UT_PRESET_LABELS.get(rv.ut_preset, "UT 音效").split("（")[0]
                elif rv.mode == "ai":
                    if rv.provider == "edge":
                        badge = "AI"
                        detail = dict(EDGE_VOICES).get(rv.voice, rv.voice).split("·")[0].strip()
                    else:
                        badge = "API"
                        detail = apis[rv.provider].name if rv.provider in apis else "自定义 API"
                else:
                    badge = "静音"
                    detail = "不配音"
                left_item = QTableWidgetItem(f"{name}　·　{count} 句")
                left_item.setData(Qt.ItemDataRole.UserRole, rid)
                left_item.setToolTip(f"{name}（{detail}，{count} 条可见台词）")
                right_item = QTableWidgetItem(badge)
                right_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.role_table.setItem(idx, 0, left_item)
                self.role_table.setItem(idx, 1, right_item)
                if rid == self.selected_role:
                    target_row = idx
            if entries:
                self.role_table.setCurrentCell(target_row, 0)

    def _role_row_changed(self, row_idx: int, _col: int, _prev_r: int, _prev_c: int) -> None:
        if not self.project or row_idx < 0:
            return
        item = self.role_table.item(row_idx, 0)
        if item is None:
            return
        self.selected_role = item.data(Qt.ItemDataRole.UserRole)
        self._refresh_role_detail()
        self._refresh_role_cues()

    def _refresh_role_detail(self) -> None:
        if not self.project:
            return
        role = self.project.roles.get(self.selected_role)
        self.cfg_role_name.setText(role.name if role else "（旁白）")
        self.cfg_role_origin.setText(
            "旁白与过场" if role is None else ("从日志导入的角色" if role.account else "手动添加的角色")
        )
        rv = role_voice_for(self.project.voice, self.selected_role)
        for key, btn in self.mode_buttons.items():
            with blocked(btn):
                btn.setChecked(rv.mode == key)
        self.mute_box.setVisible(rv.mode == "mute")
        self.ut_box.setVisible(rv.mode == "ut")
        self.ai_box.setVisible(rv.mode == "ai")

        # UT widgets
        with blocked(self.ut_preset_combo, self.ut_pitch_slider, self.ut_pace_slider, self.ut_vol_slider):
            self.ut_preset_combo.setCurrentIndex(max(0, self.ut_preset_combo.findData(rv.ut_preset)))
            self.ut_pitch_slider.setValue(rv.ut_pitch)
            self.ut_pace_slider.setValue(rv.ut_pace)
            self.ut_vol_slider.setValue(rv.volume)
        self.ut_pitch_val.setText(f"{rv.ut_pitch:+d} 半音")
        self.ut_pace_val.setText(UT_PACES[max(0, min(2, rv.ut_pace))])
        self.ut_vol_val.setText(f"{rv.volume}%")
        self.ut_custom_row.setVisible(rv.ut_preset == "custom")
        custom_asset = self.project.assets.get(rv.ut_asset)
        self.ut_custom_label.setText(custom_asset.name if custom_asset else "未选择短音效")

        # AI widgets
        apis = api_config_map(self.api_configs)
        provider_id = rv.provider if (rv.provider == "edge" or rv.provider in apis) else "edge"
        with blocked(
            self.provider_combo, self.ai_voice_combo, self.ai_voice_edit,
            self.role_ref_audio, self.role_ref_text,
            self.ai_rate_slider, self.ai_pitch_slider, self.ai_vol_slider,
        ):
            self.provider_combo.setCurrentIndex(max(0, self.provider_combo.findData(provider_id)))
            self.ai_voice_combo.clear()
            if provider_id == "edge":
                for vid, title in EDGE_VOICES:
                    self.ai_voice_combo.addItem(title, vid)
                self.ai_voice_combo.setCurrentIndex(max(0, self.ai_voice_combo.findData(rv.voice)))
            else:
                cfg = apis[provider_id]
                for choice in cfg.voice_choices():
                    self.ai_voice_combo.addItem(choice, choice)
                if (idx := self.ai_voice_combo.findData(rv.voice)) >= 0:
                    self.ai_voice_combo.setCurrentIndex(idx)
            self.ai_voice_edit.setText(rv.voice)
            self.role_ref_audio.setText(rv.ref_audio)
            self.role_ref_text.setText(rv.ref_text)
            self.ai_rate_slider.setValue(rv.rate)
            self.ai_pitch_slider.setValue(rv.pitch)
            self.ai_vol_slider.setValue(rv.volume)

        is_edge = provider_id == "edge"
        is_gpt = (not is_edge) and apis[provider_id].protocol == "gptsovits"
        has_choices = is_edge or (not is_gpt and bool(apis[provider_id].voice_choices()))
        self.ai_voice_label.setVisible(not is_gpt)
        self.ai_voice_combo.setVisible(has_choices)
        self.ai_voice_edit.setVisible((not is_edge) and (not is_gpt) and not has_choices)
        self.ref_override_box.setVisible(is_gpt)
        self.ai_rate_val.setText(f"{rv.rate:+d}%")
        self.ai_pitch_val.setText(f"{rv.pitch:+d}%")
        self.ai_vol_val.setText(f"{rv.volume}%")
        ready, total = self._ai_cache_stats(role_id=self.selected_role)
        self.ai_role_status.setText(f"已缓存 {ready} / {total} 句 · 增量复用")

    def _ai_cache_stats(self, role_id: str | None = None) -> tuple[int, int]:
        if not self.project:
            return 0, 0
        apis = api_config_map(self.api_configs)
        ready = total = 0
        voice_cfg = self.project.voice
        assets = self.project.assets
        for cue in self.project.cues:
            if role_id is not None and cue.role != role_id:
                continue
            if effective_cue_mode(self.project, cue) != "ai":
                continue
            total += 1
            cv = voice_cfg.cues.get(cue.id)
            if not (cv and cv.asset in assets and cv.duration_frames > 0 and cv.fingerprint):
                continue
            rv = role_voice_for(voice_cfg, cue.role)
            segs = prepare_spoken_segments(cue.text, voice_cfg, cv)
            fp = voice_fingerprint(segs, rv, voice_cfg.narrator, apis)
            if cv.fingerprint == fp:
                ready += 1
        return ready, total

    def _refresh_batch_progress(self) -> None:
        if self._batch_worker is not None and self._batch_worker.isRunning():
            return
        ready, total = self._ai_cache_stats()
        self.batch_progress.setRange(0, max(1, total))
        self.batch_progress.setValue(ready if total else 0)
        self.batch_progress.setFormat(f"AI 已缓存 {ready} / {total}")
        self.generate_all_btn.setText("生成全部 AI 配音")

    def _cue_status_badge(self, cue: Cue, apis: dict[str, VoiceApiConfig]) -> str:
        mode = effective_cue_mode(self.project, cue)
        cv = self.project.voice.cues.get(cue.id)
        if mode == "mute":
            return "静音" if (cv and cv.mode == "mute") else "跳过"
        if mode == "ut":
            return "UT"
        if mode == "custom":
            return "干声" if (cv and cv.asset in self.project.assets) else "缺干声"
        if not (cv and cv.asset in self.project.assets and cv.duration_frames > 0 and cv.fingerprint):
            return "待生成"
        rv = role_voice_for(self.project.voice, cue.role)
        segs = prepare_spoken_segments(cue.text, self.project.voice, cv)
        fp = voice_fingerprint(segs, rv, self.project.voice.narrator, apis)
        if cv.fingerprint == fp:
            return "已改口播" if cv.spoken_text.strip() else "已就绪"
        return "待生成"

    def _refresh_role_cues(self) -> None:
        if not self.project:
            return
        role = self.project.roles.get(self.selected_role)
        name = role.name if role else "（旁白）"
        cues = [
            cue for cue in self.project.cues
            if cue.is_text and not cue.hidden and cue.text.strip() and cue.role == self.selected_role
        ]
        apis = api_config_map(self.api_configs)
        self.cues_title.setText(f"{name}的台词")
        self.cues_count.setText(f"{len(cues)} 条 · 点选试听")
        self.cue_table.setUpdatesEnabled(False)
        try:
            with blocked(self.cue_table):
                self.cue_table.setRowCount(0)
                self.cue_table.setRowCount(len(cues))
                selected_row = -1
                for idx, cue in enumerate(cues):
                    text = " ".join(cue.text.split())
                    text_item = QTableWidgetItem(text[:80] + ("…" if len(text) > 80 else ""))
                    text_item.setData(Qt.ItemDataRole.UserRole, cue.id)
                    text_item.setToolTip(cue.text)
                    status_item = QTableWidgetItem(self._cue_status_badge(cue, apis))
                    status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.cue_table.setItem(idx, 0, text_item)
                    self.cue_table.setItem(idx, 1, status_item)
                    if cue.id == self.selected_cue_id:
                        selected_row = idx
                if selected_row >= 0:
                    self.cue_table.setCurrentCell(selected_row, 0)
        finally:
            self.cue_table.setUpdatesEnabled(True)

    def _cue_row_clicked(self, row_idx: int, _col: int) -> None:
        item = self.cue_table.item(row_idx, 0)
        if item is None:
            return
        cue_id = item.data(Qt.ItemDataRole.UserRole)
        self.selected_cue_id = cue_id
        self._refresh_cue_detail()
        if self.timeline and (frame := self.timeline.cue_frames.get(cue_id)) is not None:
            self.seek.emit(frame)

    def _selected_cue(self) -> Cue | None:
        if not self.project or not self.selected_cue_id:
            return None
        return next((c for c in self.project.cues if c.id == self.selected_cue_id), None)

    def _refresh_cue_detail(self) -> None:
        cue = self._selected_cue()
        enabled = cue is not None and cue.is_text
        for widget in (self.cue_mode_combo, self.spoken_edit, self.cue_audition_btn, self.cue_regen_btn):
            widget.setEnabled(enabled)
        if not enabled:
            self.cue_head_label.setText("选择一条台词进行单句微调")
            self.cue_origin_label.setText("")
            self.cue_custom_row.setVisible(False)
            with blocked(self.cue_mode_combo, self.spoken_edit):
                self.cue_mode_combo.setCurrentIndex(0)
                self.spoken_edit.setPlainText("")
            return
        actor = self.project.roles.get(cue.role)
        speaker = actor.name if actor else "旁白"
        self.cue_head_label.setText(f"单句微调 · {speaker}")
        snippet = " ".join(cue.text.split())
        self.cue_origin_label.setText(snippet[:90] + ("…" if len(snippet) > 90 else ""))
        cv = self.project.voice.cues.get(cue.id, CueVoice())
        with blocked(self.cue_mode_combo, self.spoken_edit):
            self.cue_mode_combo.setCurrentIndex(max(0, self.cue_mode_combo.findData(cv.mode)))
            if self.spoken_edit.toPlainText() != cv.spoken_text:
                self.spoken_edit.setPlainText(cv.spoken_text)
        self.cue_custom_row.setVisible(cv.mode == "custom")
        asset = self.project.assets.get(cv.asset)
        self.cue_custom_label.setText(asset.name if asset else "未选择本地干声")

    def _global_rule_changed(self) -> None:
        if not self.project:
            return
        s = self.project.voice
        s.skip_dice = self.skip_dice_check.isChecked()
        s.duck_music = self.duck_check.isChecked()
        s.duck_level = self.duck_spin.value()
        self.duck_spin.setEnabled(s.duck_music)
        s.auto_duration = self.auto_dur_check.isChecked()
        s.bracket_mode = self.bracket_combo.currentData()
        self._refresh_role_cues()
        self._refresh_batch_progress()
        self.changed.emit()

    def _set_role_mode(self, mode: str) -> None:
        if not self.project:
            return
        rv = ensure_role_voice(self.project.voice, self.selected_role)
        rv.mode = mode
        self._refresh_role_list()
        self._refresh_role_detail()
        self._refresh_role_cues()
        self._refresh_batch_progress()
        self.changed.emit()

    def _ut_changed(self) -> None:
        if not self.project:
            return
        rv = ensure_role_voice(self.project.voice, self.selected_role)
        rv.ut_preset = self.ut_preset_combo.currentData()
        rv.ut_pitch = self.ut_pitch_slider.value()
        rv.ut_pace = self.ut_pace_slider.value()
        rv.volume = self.ut_vol_slider.value()
        self.ut_pitch_val.setText(f"{rv.ut_pitch:+d} 半音")
        self.ut_pace_val.setText(UT_PACES[max(0, min(2, rv.ut_pace))])
        self.ut_vol_val.setText(f"{rv.volume}%")
        self.ut_custom_row.setVisible(rv.ut_preset == "custom")
        self._refresh_role_list()
        self.changed.emit()

    def _choose_ut_asset(self) -> None:
        if not self.project or not self.cache:
            return
        selected, _ = QFileDialog.getOpenFileName(
            self.right_panel, "选择短音效", "", "音频 (*.wav *.mp3 *.ogg)",
        )
        if not selected:
            return
        try:
            asset = import_asset(self.project, Path(selected), self.cache.directory, VOICE_EXTENSIONS)
            self.cache.decoded(asset.id)
        except (OSError, ReplayError) as error:
            QMessageBox.warning(self.right_panel, "音效导入失败", str(error))
            return
        rv = ensure_role_voice(self.project.voice, self.selected_role)
        rv.ut_preset = "custom"
        rv.ut_asset = asset.id
        self._refresh_role_detail()
        self.changed.emit()

    def _provider_changed(self) -> None:
        if not self.project:
            return
        rv = ensure_role_voice(self.project.voice, self.selected_role)
        rv.provider = self.provider_combo.currentData()
        apis = api_config_map(self.api_configs)
        if rv.provider == "edge":
            if rv.voice not in dict(EDGE_VOICES):
                rv.voice = EDGE_VOICES[0][0]
        elif rv.provider in apis:
            choices = apis[rv.provider].voice_choices()
            if choices and rv.voice not in choices:
                rv.voice = choices[0]
        self._refresh_role_list()
        self._refresh_role_detail()
        self._refresh_role_cues()
        self._refresh_batch_progress()
        self.changed.emit()

    def _ai_changed(self) -> None:
        if not self.project:
            return
        rv = ensure_role_voice(self.project.voice, self.selected_role)
        if self.ai_voice_combo.isVisible() and self.ai_voice_combo.currentData():
            rv.voice = self.ai_voice_combo.currentData()
        elif self.ai_voice_edit.isVisible():
            rv.voice = self.ai_voice_edit.text().strip()
        rv.ref_audio = self.role_ref_audio.text().strip()
        rv.ref_text = self.role_ref_text.text().strip()
        rv.rate = self.ai_rate_slider.value()
        rv.pitch = self.ai_pitch_slider.value()
        rv.volume = self.ai_vol_slider.value()
        self.ai_rate_val.setText(f"{rv.rate:+d}%")
        self.ai_pitch_val.setText(f"{rv.pitch:+d}%")
        self.ai_vol_val.setText(f"{rv.volume}%")
        self._refresh_role_list()
        self._refresh_role_cues()
        self._refresh_batch_progress()
        self.changed.emit()

    def _browse_role_ref(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self.right_panel, "选择角色参考音频", "", "音频 (*.wav *.mp3 *.ogg)")
        if selected:
            self.role_ref_audio.setText(selected)
            self._ai_changed()

    def _cue_mode_changed(self) -> None:
        cue = self._selected_cue()
        if not cue or not self.project:
            return
        cv = self.project.voice.cues.setdefault(cue.id, CueVoice())
        cv.mode = self.cue_mode_combo.currentData()
        self._refresh_cue_detail()
        self._refresh_role_cues()
        self._refresh_batch_progress()
        self.changed.emit()

    def _spoken_text_changed(self) -> None:
        cue = self._selected_cue()
        if not cue or not self.project:
            return
        cv = self.project.voice.cues.setdefault(cue.id, CueVoice())
        cv.spoken_text = self.spoken_edit.toPlainText()
        self._refresh_role_cues()
        self._refresh_batch_progress()
        self.changed.emit()

    def _choose_cue_audio(self) -> None:
        cue = self._selected_cue()
        if not cue or not self.project or not self.cache:
            return
        selected, _ = QFileDialog.getOpenFileName(self.right_panel, "选择本地干声", "", "音频 (*.wav *.mp3 *.ogg)")
        if not selected:
            return
        try:
            asset = import_asset(self.project, Path(selected), self.cache.directory, VOICE_EXTENSIONS)
            frames = max(1, self.cache.frame_length(asset.id))
        except (OSError, ReplayError) as error:
            QMessageBox.warning(self.right_panel, "干声导入失败", str(error))
            return
        cv = self.project.voice.cues.setdefault(cue.id, CueVoice())
        cv.mode = "custom"
        cv.asset = asset.id
        cv.duration_frames = frames
        cv.fingerprint = ""
        self._refresh_cue_detail()
        self._refresh_role_cues()
        self.changed.emit()

    def open_api_dialog(self) -> None:
        dialog = VoiceApiDialog(self.audio, self.parent())
        if dialog.exec() == VoiceApiDialog.DialogCode.Accepted:
            self._refresh_providers()
            if self.project:
                self._refresh_role_list()
                self._refresh_role_detail()
                self._refresh_batch_progress()
            self.notice.emit("语音接口配置已保存")
        dialog.deleteLater()

    def _sample_text_for_role(self, role_id: str) -> str:
        if self.project:
            for cue in self.project.cues:
                if cue.is_text and not cue.hidden and cue.role == role_id:
                    segs = prepare_spoken_segments(cue.text, self.project.voice, self.project.voice.cues.get(cue.id))
                    if segs:
                        return segs[0][1][:40]
        return "等等，门后好像有声音，我们先仔细听一听。"

    def _audition_ut(self) -> None:
        if not self.project or not self.cache:
            return
        rv = role_voice_for(self.project.voice, self.selected_role)
        custom_pcm = None
        if rv.ut_preset == "custom" and rv.ut_asset in self.project.assets:
            try:
                custom_pcm = self.cache.decoded(rv.ut_asset)
            except (OSError, ReplayError) as error:
                self.notice.emit(str(error))
                return
        pcm = render_ut_pcm(self._sample_text_for_role(self.selected_role), rv, 45, custom_pcm)
        self.audio.play_pcm(pcm)

    def _start_ai_audition(self, text: str, rv: RoleVoice) -> None:
        if self._audition_worker is not None and self._audition_worker.isRunning():
            return
        self.notice.emit("正在合成试听音频…")
        self.ai_audition_btn.setEnabled(False)
        self.cue_audition_btn.setEnabled(False)
        self._audition_worker = _VoiceAuditionWorker(text, replace(rv), api_config_map(self.api_configs), self)
        self._audition_worker.ready.connect(self._on_audition_ready)
        self._audition_worker.failed.connect(self._on_audition_failed)
        self._audition_worker.finished.connect(self._on_audition_finished)
        self._audition_worker.start()

    def _on_audition_ready(self, pcm: bytes) -> None:
        self.audio.play_pcm(pcm)
        self.notice.emit("正在试听配音")

    def _on_audition_failed(self, message: str) -> None:
        self.notice.emit(f"试听失败　{message}")
        QMessageBox.warning(self.right_panel, "试听失败", message)

    def _on_audition_finished(self) -> None:
        self.ai_audition_btn.setEnabled(True)
        self.cue_audition_btn.setEnabled(self._selected_cue() is not None)

    def _audition_ai(self) -> None:
        if not self.project:
            return
        rv = role_voice_for(self.project.voice, self.selected_role)
        self._start_ai_audition(self._sample_text_for_role(self.selected_role), rv)

    def _audition_current_cue(self) -> None:
        cue = self._selected_cue()
        if not cue or not self.project or not self.cache:
            return
        mode = effective_cue_mode(self.project, cue)
        cv = self.project.voice.cues.get(cue.id)
        rv = role_voice_for(self.project.voice, cue.role)
        if mode == "mute":
            self.notice.emit("该句当前为静音或被朗读规则跳过")
            return
        if mode == "ut":
            segs = prepare_spoken_segments(cue.text, self.project.voice, cv)
            text = " ".join(part for _, part in segs) or cue.text
            custom_pcm = self.cache.decoded(rv.ut_asset) if (rv.ut_preset == "custom" and rv.ut_asset in self.project.assets) else None
            self.audio.play_pcm(render_ut_pcm(text, rv, 60, custom_pcm))
            return
        if cv and cv.asset in self.project.assets:
            try:
                pcm_path = self.cache.decoded(cv.asset)
                self.audio.play_pcm(pcm_path.read_bytes())
                return
            except (OSError, ReplayError):
                pass
        segs = prepare_spoken_segments(cue.text, self.project.voice, cv)
        if segs:
            self._start_ai_audition("，".join(part for _, part in segs), rv)

    def _regen_current_cue(self) -> None:
        cue = self._selected_cue()
        if not cue or not self.project:
            return
        if cv := self.project.voice.cues.get(cue.id):
            cv.fingerprint = ""
        self._start_batch([cue])

    def _generate_selected_role(self) -> None:
        if not self.project:
            return
        cues = [
            cue for cue in self.project.cues
            if cue.role == self.selected_role and effective_cue_mode(self.project, cue) == "ai"
        ]
        if not cues:
            self.notice.emit("该角色没有需要生成的 AI 配音台词")
            return
        self._start_batch(cues)

    def _toggle_batch_all(self) -> None:
        if self._batch_worker is not None and self._batch_worker.isRunning():
            self._batch_worker.stop.set()
            self.generate_all_btn.setEnabled(False)
            self.notice.emit("正在停止配音生成…")
            return
        if not self.project:
            return
        cues = [cue for cue in self.project.cues if effective_cue_mode(self.project, cue) == "ai"]
        if not cues:
            self.notice.emit("请先将至少一个角色的发声方式设为「AI 人声」")
            return
        self._start_batch(cues)

    def _start_batch(self, cues: list[Cue]) -> None:
        if not self.project or not self.cache:
            return
        if self._batch_worker is not None and self._batch_worker.isRunning():
            self.notice.emit("已有配音任务正在运行")
            return
        self.generate_all_btn.setText("停止生成")
        self.ai_gen_role_btn.setEnabled(False)
        self.cue_regen_btn.setEnabled(False)
        self.batch_progress.setRange(0, max(1, len(cues)))
        self.batch_progress.setValue(0)
        self._batch_worker = _VoiceBatchWorker(
            self.project, cues, self.cache.directory, self.cache,
            api_config_map(self.api_configs), self,
        )
        self._batch_worker.item_done.connect(self._on_batch_item)
        self._batch_worker.progress.connect(self._on_batch_progress)
        self._batch_worker.finished_summary.connect(self._on_batch_finished)
        self._batch_worker.start()

    def _on_batch_item(self, cue_id: str, asset_id: str, fingerprint: str, frames: int) -> None:
        if not self.project:
            return
        cv = self.project.voice.cues.setdefault(cue_id, CueVoice())
        cv.asset = asset_id
        cv.fingerprint = fingerprint
        cv.duration_frames = frames

    def _on_batch_progress(self, current: int, total: int, message: str) -> None:
        self.batch_progress.setRange(0, max(1, total))
        self.batch_progress.setValue(current)
        self.batch_progress.setFormat(message or f"AI 配音 {current} / {total}")

    def _on_batch_finished(self, generated: int, skipped: int, error_text: str) -> None:
        self.generate_all_btn.setEnabled(True)
        self.ai_gen_role_btn.setEnabled(True)
        self.cue_regen_btn.setEnabled(self._selected_cue() is not None)
        self._refresh_role_detail()
        self._refresh_role_cues()
        self._refresh_batch_progress()
        if generated:
            self.changed.emit()
        if error_text:
            self.notice.emit(f"配音已生成 {generated} 句（跳过缓存 {skipped} 句），遇到错误：{error_text}")
            QMessageBox.warning(self.left_panel, "部分配音生成失败", error_text)
        else:
            self.notice.emit(f"配音完成：新生成 {generated} 句，复用缓存 {skipped} 句")

    def stop_workers(self) -> None:
        if self._batch_worker is not None and self._batch_worker.isRunning():
            self._batch_worker.stop.set()
            self._batch_worker.wait(1500)
        if self._audition_worker is not None and self._audition_worker.isRunning():
            self._audition_worker.wait(1000)
