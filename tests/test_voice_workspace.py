from __future__ import annotations

import http.server
import json
import math
import os
import subprocess
import tempfile
import threading
import unittest
import wave
import zipfile
from array import array
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from replaystudio.appearance import configure_appearance
from replaystudio.audio import AudioCache, AudioMixer, SAMPLE_RATE, SAMPLES_PER_FRAME
from replaystudio.exporting import ExportJob, find_ffmpeg
from replaystudio.media_view import VOICE_TRACK_ID
from replaystudio.model import (
    FPS, CueVoice, MediaClip, MediaEdit, RoleVoice, VoiceSettings, project_from_log,
)
from replaystudio.render import build_timeline, initialize_media
from replaystudio.storage import VOICE_EXTENSIONS, import_bytes, open_project, save_project
from replaystudio.voice import (
    API_SETTINGS_KEY, VoiceApiConfig, load_api_configs, new_api_config,
    prepare_spoken_segments, render_ut_pcm, save_api_configs,
    synthesize_api_bytes, synthesize_cue_audio, test_api_connection,
)
from replaystudio.voice_api_dialog import VoiceApiDialog
from replaystudio.window import MainWindow

SAMPLE_LOG = """叶之助(1001) 2026/11/22 20:00:00
（压低声音）等等，门后有声音！

妮娅(1002) 2026/11/22 20:00:01
.rd100 侦查

妮娅(1002) 2026/11/22 20:00:02
<叶之助> 对侦查进行检定：32/70 成功

萨尔(1003) 2026/11/22 20:00:04
我先架好盾牌。
""".encode("utf-8")


def wav_bytes(hz: int = 440, seconds: float = 0.5) -> bytes:
    rate = 24_000
    count = int(rate * seconds)
    samples = array("h", (round(12_000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(count)))
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, rate, len(samples), "NONE", "not compressed"))
            output.writeframes(samples.tobytes())
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def make_project():
    project = project_from_log(SAMPLE_LOG, "声音测试.txt", "声音测试")
    initialize_media(project)
    return project


class VoiceWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_appearance(cls.app)

    def setUp(self):
        self._store = QSettings("ReplayStudio", "ReplayStudio")
        self._saved_apis = self._store.value(API_SETTINGS_KEY, "", type=str)
        self._store.remove(API_SETTINGS_KEY)

    def tearDown(self):
        if self._saved_apis:
            self._store.setValue(API_SETTINGS_KEY, self._saved_apis)
        else:
            self._store.remove(API_SETTINGS_KEY)

    def test_text_cleaning_dice_brackets_and_spoken_override(self):
        settings = VoiceSettings(skip_dice=True, bracket_mode="skip")
        self.assertEqual(prepare_spoken_segments(".rd100 侦查", settings), ())
        self.assertEqual(prepare_spoken_segments("<叶之助> 对侦查进行检定", settings), ())
        self.assertEqual(
            prepare_spoken_segments("（压低声音）等等，门后有声音！", settings),
            (("role", "等等，门后有声音！"),),
        )
        # Unclosed OOC brackets (common in QQ TRPG logs)
        self.assertEqual(prepare_spoken_segments("（略有同见", settings), ())
        self.assertEqual(prepare_spoken_segments("（bz剧情（hyw", settings), ())
        self.assertEqual(prepare_spoken_segments("（痴汉？！（bushi）", settings), ())
        self.assertEqual(
            prepare_spoken_segments("等等！（场外：我去倒水", settings),
            (("role", "等等！"),),
        )

        settings.bracket_mode = "keep"
        self.assertEqual(
            prepare_spoken_segments("（压低声音）等等！", settings),
            (("role", "压低声音 等等！"),),
        )

        settings.bracket_mode = "narrator"
        self.assertEqual(
            prepare_spoken_segments("（压低声音）等等，门后有声音！（握紧手电）", settings),
            (("narrator", "压低声音"), ("role", "等等，门后有声音！"), ("narrator", "握紧手电")),
        )
        self.assertEqual(
            prepare_spoken_segments("等等！（场外：我去倒水", settings),
            (("role", "等等！"), ("narrator", "场外：我去倒水")),
        )

        override = CueVoice(spoken_text="理智检定九十九")
        self.assertEqual(
            prepare_spoken_segments(".sc 1/1d6", settings, override),
            (("role", "理智检定九十九"),),
        )

    def test_ut_synth_presets_and_export_with_ducking(self):
        project = make_project()
        role_ids = list(project.roles)
        ye_id = role_ids[0]
        project.voice.roles[ye_id] = RoleVoice(mode="ut", ut_preset="pixel", ut_pitch=3, ut_pace=1, volume=85)

        for preset in ("crisp", "deep", "bright", "pixel", "typewriter"):
            pcm = render_ut_pcm("测试台词内容", RoleVoice(mode="ut", ut_preset=preset, ut_pitch=2), 30)
            self.assertEqual(len(pcm), 30 * SAMPLES_PER_FRAME * 4)
            samples = array("h")
            samples.frombytes(pcm)
            self.assertGreater(max(abs(v) for v in samples), 1000)

        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder)
            bgm_asset = import_bytes(project, wav_bytes(220, 2.0), "bgm.wav", ".wav", media)
            project.media.library.append(bgm_asset.id)
            project.media.clips.append(MediaClip("bgm1", "music", bgm_asset.id, 0, 120, volume=100, loop=True))

            timeline = build_timeline(project)
            self.assertEqual(len(timeline.voices), 1)
            self.assertEqual(timeline.voices[0].mode, "ut")
            self.assertTrue(timeline.duck_spans)

            # Verify BGM ducking lowers music during voice span and stays full outside
            cache = AudioCache(project, media)
            bgm_pcm = cache.decoded(bgm_asset.id)
            clips = (MediaClip("bgm1", "music", bgm_asset.id, 0, 180, volume=100, loop=True),)
            mixer = AudioMixer(clips, {bgm_asset.id: bgm_pcm}, duck_spans=((30, 90),),
                               duck_tracks=frozenset({"music"}), duck_gain=0.35)
            loud = array("h")
            loud.frombytes(mixer.read(5 * SAMPLES_PER_FRAME, SAMPLES_PER_FRAME))
            ducked = array("h")
            ducked.frombytes(mixer.read(60 * SAMPLES_PER_FRAME, SAMPLES_PER_FRAME))
            self.assertLess(max(abs(v) for v in ducked), max(abs(v) for v in loud) * 0.5)

            # Export MP4 with UT voice + ducked BGM
            movie = media / "ut-out.mp4"
            job = ExportJob(project, timeline, media, 0, min(60, timeline.length), movie, lambda *_: None)
            job.run()
            self.assertTrue(movie.is_file() and movie.stat().st_size > 1024)
            probed = subprocess.check_output([
                find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(movie),
                "-map", "0:a:0", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", "pipe:1",
            ])
            audio_samples = array("h")
            audio_samples.frombytes(probed[: SAMPLE_RATE * 4])
            self.assertGreater(max(abs(v) for v in audio_samples), 1000)

    def test_custom_api_protocols_duration_alignment_and_secure_package(self):
        received: list[dict] = []
        audio_payload = wav_bytes(520, 1.8)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                received.append({"path": self.path, "auth": self.headers.get("Authorization", ""), "body": body})
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio_payload)))
                self.end_headers()
                self.wfile.write(audio_payload)

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            gpt_cfg = new_api_config("gptsovits", "本机测试")
            gpt_cfg.url = f"http://127.0.0.1:{port}/tts"
            gpt_cfg.api_key = "super-secret-token"
            gpt_cfg.ref_audio = "ref.wav"

            openai_cfg = new_api_config("openai", "OpenAI兼容")
            openai_cfg.url = f"http://127.0.0.1:{port}/v1/audio/speech"
            openai_cfg.api_key = "sk-test-123"

            custom_cfg = new_api_config("custom", "通用模板")
            custom_cfg.url = f"http://127.0.0.1:{port}/custom"

            data, ext, latency = test_api_connection(gpt_cfg)
            self.assertEqual(ext, ".wav")
            self.assertGreater(len(data), 1000)
            self.assertGreaterEqual(latency, 1)
            self.assertEqual(received[-1]["auth"], "Bearer super-secret-token")
            self.assertEqual(received[-1]["body"]["ref_audio_path"], "ref.wav")

            data2, ext2 = synthesize_api_bytes(openai_cfg, "测试句子", RoleVoice(mode="ai", provider=openai_cfg.id, voice="nova"))
            self.assertEqual(ext2, ".wav")
            self.assertEqual(received[-1]["body"]["voice"], "nova")

            data3, _ = synthesize_api_bytes(custom_cfg, "测试模板", RoleVoice(mode="ai", provider=custom_cfg.id, voice="kp"))
            self.assertGreater(len(data3), 1000)
            self.assertEqual(received[-1]["body"]["speaker"], "kp")

            # Synthesize a cue, verify auto_duration expands timeline, and verify .replay never leaks API keys
            save_api_configs([gpt_cfg])
            self.assertEqual(len(load_api_configs()), 1)

            project = make_project()
            ye_id = next(iter(project.roles))
            project.voice.roles[ye_id] = RoleVoice(mode="ai", provider=gpt_cfg.id, volume=95)
            project.voice.auto_duration = True
            project.voice.gap_frames = 9

            with tempfile.TemporaryDirectory() as folder:
                media = Path(folder)
                cache = AudioCache(project, media)
                cue = project.cues[0]
                asset, fp, frames = synthesize_cue_audio(project, cue, media, cache, {gpt_cfg.id: gpt_cfg})
                self.assertGreaterEqual(frames, int(1.7 * FPS))
                project.voice.cues[cue.id] = CueVoice(asset=asset.id, fingerprint=fp, duration_frames=frames)

                # Cache hit skips network call
                before_calls = len(received)
                asset_again, _, _ = synthesize_cue_audio(project, cue, media, cache, {gpt_cfg.id: gpt_cfg})
                self.assertEqual(asset_again.id, asset.id)
                self.assertEqual(len(received), before_calls)

                timeline = build_timeline(project)
                first_page_end = timeline.pages[0].end
                self.assertEqual(first_page_end, frames + 9)
                self.assertEqual(len(timeline.voices), 1)

                bundle_path = media / "voice-package.replay"
                save_project(project, bundle_path, media)
                with zipfile.ZipFile(bundle_path) as zf:
                    raw_json = zf.read("project.json").decode("utf-8")
                    self.assertNotIn("super-secret-token", raw_json)
                    self.assertIn(f"assets/{asset.stored_name}", zf.namelist())

                reopen_dir = media / "reopen"
                reopen_dir.mkdir()
                reopened = open_project(bundle_path, reopen_dir)
                self.assertEqual(reopened.voice.roles[ye_id].mode, "ai")
                self.assertEqual(reopened.voice.cues[cue.id].asset, asset.id)
                self.assertIn(asset.id, reopened.assets)
        finally:
            server.shutdown()
            server.server_close()

    def test_window_voice_workspace_and_api_dialog(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "声音测试.txt"
            path.write_bytes(SAMPLE_LOG)
            window = MainWindow()
            window.resize(1280, 760)
            window.show()
            self.addCleanup(window.deleteLater)
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
                try:
                    self.assertEqual(
                        [window.workspace_tabs.tabText(i) for i in range(window.workspace_tabs.count())],
                        ["台词", "剪辑", "声音"],
                    )
                    self.assertFalse(window.workspace_tabs.isTabEnabled(2))
                    self.assertTrue(window.load_path(path))
                    self.assertTrue(window.workspace_tabs.isTabEnabled(2))

                    # Switch to 声音 workspace
                    window.workspace_tabs.setCurrentIndex(2)
                    self.app.processEvents()
                    self.assertTrue(window.voice_editor.left_panel.isVisible())
                    self.assertTrue(window.voice_editor.right_panel.isVisible())
                    self.assertTrue(window.voice_editor.cues_panel.isVisible())

                    # Enable UT sound for first role and verify timeline + 剪辑 reference track
                    window.voice_editor.mode_buttons["ut"].click()
                    window._ensure_rebuilt()
                    self.app.processEvents()
                    self.assertEqual(len(window.timeline.voices), 1)
                    self.assertEqual(window.timeline.voices[0].mode, "ut")

                    # Check 剪辑 workspace displays read-only 角色配音 track
                    window.workspace_tabs.setCurrentIndex(1)
                    self.app.processEvents()
                    track_ids = [row.track.id for row in window.media_editor.timeline.rows]
                    self.assertIn(VOICE_TRACK_ID, track_ids)

                    # Switch back to 声音 and edit single-cue spoken text override
                    window.workspace_tabs.setCurrentIndex(2)
                    self.app.processEvents()
                    window.voice_editor.cue_table.cellClicked.emit(0, 0)
                    window.voice_editor.right_tabs.setCurrentIndex(1)
                    window.voice_editor.spoken_edit.setPlainText("等等，门后真的有声音")
                    self.app.processEvents()
                    self.assertEqual(window.project.voice.cues["m1"].spoken_text, "等等，门后真的有声音")
                    self.assertEqual(window.project.cues[0].text, "（压低声音）等等，门后有声音！")

                    # Verify VoiceApiDialog creates and persists a custom API config
                    dialog = VoiceApiDialog(window.audio, window)
                    dialog._add_config("gptsovits")
                    dialog.name_edit.setText("测试部署模型")
                    dialog.url_edit.setText("http://127.0.0.1:9880/tts")
                    dialog._save_and_accept()
                    dialog.deleteLater()

                    saved = load_api_configs()
                    self.assertEqual(len(saved), 1)
                    self.assertEqual(saved[0].name, "测试部署模型")
                    self.assertEqual(saved[0].protocol, "gptsovits")
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()
