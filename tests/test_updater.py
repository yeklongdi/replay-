from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from replaystudio.appearance import configure_appearance
from replaystudio.model import ReplayError
from replaystudio.update_dialog import UpdateDialog
from replaystudio.updater import (
    InstallTarget, ReleaseInfo, UpdateCancelled, detect_install_target, download_release,
    fetch_latest_release, is_newer, launch_windows_updater, stage_update_bundle, validate_url,
    write_updater_script,
)
from replaystudio.version import APP_VERSION, CHECKSUM_ASSET_NAME, PORTABLE_ASSET_NAME
from replaystudio.window import MainWindow


def make_portable_zip(destination: Path, exe_bytes: bytes = b"NEW-EXE", dll_bytes: bytes = b"NEW-DLL") -> str:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("ReplayStudio/ReplayStudio.exe", exe_bytes)
        bundle.writestr("ReplayStudio/_internal/runtime.dll", dll_bytes)
        bundle.writestr("ReplayStudio/使用说明.txt", "新版说明")
    return hashlib.sha256(destination.read_bytes()).hexdigest()


class _MockReleaseServer:
    def __init__(self, routes: dict[str, tuple[int, bytes, str]]):
        self.routes = routes
        server_ref = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                status, body, ctype = server_ref.routes.get(self.path, (404, b'{"message":"Not Found"}', "application/json"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


class UpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure_appearance(cls.app)

    def test_version_comparison_handles_semver_and_prereleases(self):
        self.assertTrue(is_newer("1.0.1", "1.0.0"))
        self.assertTrue(is_newer("v1.10.0", "1.2.0"))
        self.assertTrue(is_newer("1.0.0", "1.0.0-beta.1"))
        self.assertTrue(is_newer("1.0.1-alpha.1", "1.0.0"))
        self.assertFalse(is_newer("v1.0.0", "1.0.0"))
        self.assertFalse(is_newer("0.9.9", "1.0.0"))
        self.assertFalse(is_newer("invalid-tag", "1.0.0"))

    def test_untrusted_download_urls_are_rejected(self):
        self.assertEqual(
            validate_url("https://github.com/yeklongdi/replay-/releases/latest"),
            "https://github.com/yeklongdi/replay-/releases/latest",
        )
        with self.assertRaises(ReplayError):
            validate_url("http://github.com/yeklongdi/replay-/releases/latest")
        with self.assertRaises(ReplayError):
            validate_url("https://evil.example.com/ReplayStudio.zip")

    def test_fetch_download_checksum_and_stage_against_local_http_server(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            zip_file = root / "release.zip"
            digest = make_portable_zip(zip_file)
            zip_bytes = zip_file.read_bytes()
            sums_bytes = f"{digest}  {PORTABLE_ASSET_NAME}\n".encode("utf-8")

            routes: dict[str, tuple[int, bytes, str]] = {}
            with _MockReleaseServer(routes) as server:
                payload = {
                    "tag_name": "v1.2.0",
                    "name": "回放工坊 v1.2.0",
                    "body": "- 新增一键更新\n- 优化轨道体验",
                    "html_url": f"{server.base_url}/releases/v1.2.0",
                    "assets": [
                        {
                            "name": PORTABLE_ASSET_NAME,
                            "size": len(zip_bytes),
                            "browser_download_url": f"{server.base_url}/files/portable.zip",
                        },
                        {
                            "name": CHECKSUM_ASSET_NAME,
                            "size": len(sums_bytes),
                            "browser_download_url": f"{server.base_url}/files/SHA256SUMS.txt",
                        },
                    ],
                }
                routes["/repos/yeklongdi/replay-/releases/latest"] = (
                    200, json.dumps(payload).encode("utf-8"), "application/json"
                )
                routes["/files/portable.zip"] = (200, zip_bytes, "application/zip")
                routes["/files/SHA256SUMS.txt"] = (200, sums_bytes, "text/plain")

                release = fetch_latest_release(api_base=server.base_url, allow_local_http=True)
                self.assertEqual(release.version, "1.2.0")
                self.assertEqual(release.sha256, digest)

                seen_progress = []
                staging = root / "staging"
                downloaded = download_release(
                    release, staging,
                    progress=lambda done, total: seen_progress.append((done, total)),
                    allow_local_http=True,
                )
                self.assertTrue(downloaded.is_file())
                self.assertEqual(seen_progress[-1], (len(zip_bytes), len(zip_bytes)))

                bundle_root = stage_update_bundle(downloaded, staging)
                self.assertEqual((bundle_root / "ReplayStudio.exe").read_bytes(), b"NEW-EXE")
                self.assertEqual((bundle_root / "_internal" / "runtime.dll").read_bytes(), b"NEW-DLL")

                # Tampered SHA-256 must fail and clean up partial download
                bad_release = replace(release, sha256="0" * 64)
                bad_staging = root / "bad-staging"
                with self.assertRaises(ReplayError):
                    download_release(bad_release, bad_staging, allow_local_http=True)
                self.assertFalse((bad_staging / "package.zip").exists())
                self.assertFalse((bad_staging / "package.zip.part").exists())

                # Pre-set cancellation event raises UpdateCancelled immediately
                cancel = threading.Event()
                cancel.set()
                with self.assertRaises(UpdateCancelled):
                    download_release(release, root / "cancelled", cancel=cancel, allow_local_http=True)

                # 404 when no release exists
                del routes["/repos/yeklongdi/replay-/releases/latest"]
                with self.assertRaises(ReplayError) as ctx:
                    fetch_latest_release(api_base=server.base_url, allow_local_http=True)
                self.assertIn("还没有发布", str(ctx.exception))

    def test_zip_slip_and_incomplete_archive_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            evil = root / "evil.zip"
            with zipfile.ZipFile(evil, "w") as bundle:
                bundle.writestr("../escape.txt", "boom")
            with self.assertRaises(ReplayError):
                stage_update_bundle(evil, root / "s1")
            self.assertFalse((root / "escape.txt").exists())

            incomplete = root / "incomplete.zip"
            with zipfile.ZipFile(incomplete, "w") as bundle:
                bundle.writestr("ReplayStudio/ReplayStudio.exe", b"EXE-ONLY")
            with self.assertRaises(ReplayError):
                stage_update_bundle(incomplete, root / "s2")

    @unittest.skipUnless(sys.platform == "win32", "Windows updater script test")
    def test_windows_updater_script_swaps_files_preserves_user_data_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "便携版 中文目录"
            (target / "_internal").mkdir(parents=True)
            (target / "ReplayStudio.exe").write_bytes(b"OLD-EXE")
            (target / "_internal" / "old.dll").write_bytes(b"OLD-DLL")
            (target / "我的跑团.replay").write_bytes(b"USER-PROJECT")

            staging = root / "staging"
            source = staging / "extracted" / "ReplayStudio"
            (source / "_internal").mkdir(parents=True)
            (source / "ReplayStudio.exe").write_bytes(b"NEW-EXE")
            (source / "_internal" / "new.dll").write_bytes(b"NEW-DLL")
            (source / "使用说明.txt").write_text("v1.2.0", encoding="utf-8")

            script = write_updater_script(staging)
            # Launch a brief dummy process so -WaitPid actually exercises process-exit waiting
            dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.35)"])
            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script),
                    "-WaitPid", str(dummy.pid),
                    "-TargetDir", str(target),
                    "-SourceDir", str(source),
                    "-ExeName", "ReplayStudio.exe",
                    "-NoRelaunch",
                ],
                capture_output=True, text=True, timeout=30,
            )
            dummy.wait(timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((target / "ReplayStudio.exe").read_bytes(), b"NEW-EXE")
            self.assertEqual((target / "_internal" / "new.dll").read_bytes(), b"NEW-DLL")
            self.assertFalse((target / "_internal" / "old.dll").exists())
            self.assertEqual((target / "我的跑团.replay").read_bytes(), b"USER-PROJECT")
            self.assertEqual(list(target.glob(".update-backup-*")), [])

            # Rollback test: broken update source missing ReplayStudio.exe must restore previous version
            broken = root / "broken-source"
            (broken / "_internal").mkdir(parents=True)
            (broken / "_internal" / "bad.dll").write_bytes(b"BAD")
            rollback = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script),
                    "-TargetDir", str(target),
                    "-SourceDir", str(broken),
                    "-ExeName", "ReplayStudio.exe",
                    "-NoRelaunch",
                ],
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(rollback.returncode, 0)
            self.assertEqual((target / "ReplayStudio.exe").read_bytes(), b"NEW-EXE")
            self.assertEqual((target / "_internal" / "new.dll").read_bytes(), b"NEW-DLL")
            self.assertEqual((target / "我的跑团.replay").read_bytes(), b"USER-PROJECT")
            self.assertEqual(list(target.glob(".update-backup-*")), [])

    @unittest.skipUnless(sys.platform == "win32", "Windows windowed updater test")
    def test_launch_windows_updater_succeeds_from_windowed_process(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("需要 pythonw.exe")
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "便携版 目录"
            (target / "_internal").mkdir(parents=True)
            (target / "ReplayStudio.exe").write_bytes(b"OLD-EXE")
            (target / "_internal" / "old.dll").write_bytes(b"OLD-DLL")
            (target / "我的跑团.replay").write_bytes(b"USER-PROJECT")

            staging = root / "staging"
            source = staging / "extracted" / "ReplayStudio"
            (source / "_internal").mkdir(parents=True)
            (source / "ReplayStudio.exe").write_bytes(b"NEW-EXE")
            (source / "_internal" / "new.dll").write_bytes(b"NEW-DLL")

            launcher = (
                "import os, sys\n"
                f"sys.path.insert(0, {str(repo_root)!r})\n"
                "from pathlib import Path\n"
                "from replaystudio.updater import launch_windows_updater\n"
                "launch_windows_updater(\n"
                f"    target_dir=Path({str(target)!r}),\n"
                f"    source_dir=Path({str(source)!r}),\n"
                f"    staging_dir=Path({str(staging)!r}),\n"
                "    wait_pid=os.getpid(),\n"
                "    no_relaunch=True,\n"
                ")\n"
            )
            subprocess.run([str(pythonw), "-c", launcher], check=True, timeout=10)

            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                exe = target / "ReplayStudio.exe"
                if exe.is_file() and exe.read_bytes() == b"NEW-EXE" and not staging.exists():
                    break
                time.sleep(0.1)

            self.assertEqual((target / "ReplayStudio.exe").read_bytes(), b"NEW-EXE")
            self.assertEqual((target / "_internal" / "new.dll").read_bytes(), b"NEW-DLL")
            self.assertFalse((target / "_internal" / "old.dll").exists())
            self.assertEqual((target / "我的跑团.replay").read_bytes(), b"USER-PROJECT")
            self.assertFalse(staging.exists())
            self.assertEqual(list(target.glob(".update-backup-*")), [])

    def test_settings_menu_entry_and_update_dialog_gui_flow(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            zip_file = root / "release.zip"
            digest = make_portable_zip(zip_file)
            zip_bytes = zip_file.read_bytes()

            # Ensure MainWindow does NOT auto-check network on startup, and places update at bottom of Settings
            with patch("replaystudio.updater.fetch_latest_release") as fetch_mock:
                window = MainWindow()
                window.show()
                self.addCleanup(window.deleteLater)
                self.addCleanup(lambda: self._close(window))
                self.app.processEvents()
                fetch_mock.assert_not_called()
                menu_texts = [a.text() for a in window.settings_button.menu().actions() if a.text()]
                self.assertEqual(menu_texts[-1], f"检查更新（v{APP_VERSION}）")

            routes: dict[str, tuple[int, bytes, str]] = {}
            with _MockReleaseServer(routes) as server:
                payload = {
                    "tag_name": "v2.0.0",
                    "name": "回放工坊 v2.0.0",
                    "body": "支持一键检查并自动更新。",
                    "html_url": f"{server.base_url}/releases/v2.0.0",
                    "assets": [{
                        "name": PORTABLE_ASSET_NAME,
                        "size": len(zip_bytes),
                        "digest": f"sha256:{digest}",
                        "browser_download_url": f"{server.base_url}/files/portable.zip",
                    }],
                }
                routes["/repos/yeklongdi/replay-/releases/latest"] = (
                    200, json.dumps(payload).encode("utf-8"), "application/json"
                )
                routes["/files/portable.zip"] = (200, zip_bytes, "application/zip")

                # 1. Source mode hides in-place install button and explains why
                source_dialog = UpdateDialog(
                    current_version="1.0.0",
                    api_base=server.base_url,
                    allow_local_http=True,
                    install_target=detect_install_target(frozen=False),
                )
                source_dialog.show()
                self.addCleanup(source_dialog.deleteLater)
                self._wait_until(lambda: source_dialog.release is not None)
                self.assertIn("发现新版本 v2.0.0", source_dialog.headline.text())
                self.assertFalse(source_dialog.install_button.isVisible())
                self.assertTrue(source_dialog.open_page_button.isVisible())
                self.assertIn("源码", source_dialog.status_label.text())
                source_dialog.reject()

                # 2. Frozen portable mode downloads, verifies SHA-256, and accepts with staged bundle
                fake_install = root / "fake-install"
                fake_install.mkdir()
                writable_target = InstallTarget(fake_install, fake_install / "ReplayStudio.exe", True, True, "")
                portable_dialog = UpdateDialog(
                    current_version="1.0.0",
                    api_base=server.base_url,
                    allow_local_http=True,
                    install_target=writable_target,
                )
                portable_dialog.show()
                self.addCleanup(portable_dialog.deleteLater)
                self._wait_until(lambda: portable_dialog.release is not None)
                self.assertTrue(portable_dialog.install_button.isVisible())
                QTest.mouseClick(portable_dialog.install_button, Qt.MouseButton.LeftButton)
                self._wait_until(lambda: portable_dialog.result() == QDialog.DialogCode.Accepted)
                self.assertIsNotNone(portable_dialog.staged_bundle)
                bundle_root, staging_dir = portable_dialog.staged_bundle
                self.assertTrue((bundle_root / "ReplayStudio.exe").is_file())
                # Clean up staging dir used by test
                import shutil
                shutil.rmtree(staging_dir, ignore_errors=True)

    def _wait_until(self, predicate, timeout_ms: int = 5000) -> None:
        elapsed = 0
        while not predicate() and elapsed < timeout_ms:
            QTest.qWait(25)
            elapsed += 25
        self.assertTrue(predicate(), "Timed out waiting for async update UI state")

    @staticmethod
    def _close(window):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
            window.close()
