from __future__ import annotations

import shutil
from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QPlainTextEdit, QProgressBar, QVBoxLayout, QWidget,
)

from .inspector import label
from .model import ReplayError
from .updater import (
    GITHUB_API_BASE, InstallTarget, ReleaseInfo, UpdateCancelled, detect_install_target,
    download_release, fetch_latest_release, format_size, is_newer, make_staging_dir,
    stage_update_bundle,
)
from .version import APP_VERSION, GITHUB_REPO, RELEASES_PAGE_URL
from .widgets import button, panel


class _CheckWorker(QThread):
    found = Signal(object)
    failed = Signal(str)

    def __init__(self, repo: str, api_base: str, allow_local_http: bool, parent: QWidget):
        super().__init__(parent)
        self.repo = repo
        self.api_base = api_base
        self.allow_local_http = allow_local_http

    def run(self) -> None:
        try:
            release = fetch_latest_release(
                repo=self.repo,
                api_base=self.api_base,
                allow_local_http=self.allow_local_http,
            )
            self.found.emit(release)
        except Exception as error:
            self.failed.emit(str(error) or type(error).__name__)


class _DownloadWorker(QThread):
    progress = Signal(int, int)
    phase = Signal(str)
    ready = Signal(object, object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(
        self,
        release: ReleaseInfo,
        staging_dir: Path,
        allow_local_http: bool,
        parent: QWidget,
    ):
        super().__init__(parent)
        self.release = release
        self.staging_dir = staging_dir
        self.allow_local_http = allow_local_http
        self.cancel_event = Event()

    def run(self) -> None:
        try:
            self.phase.emit("正在下载新版本安装包…")
            zip_path = download_release(
                self.release,
                self.staging_dir,
                cancel=self.cancel_event,
                progress=self.progress.emit,
                allow_local_http=self.allow_local_http,
            )
            if self.cancel_event.is_set():
                raise UpdateCancelled()
            self.phase.emit("正在校验文件完整性并准备解压…")
            bundle_root = stage_update_bundle(zip_path, self.staging_dir)
            if self.cancel_event.is_set():
                raise UpdateCancelled()
            self.ready.emit(bundle_root, self.staging_dir)
        except UpdateCancelled:
            self.cancelled.emit()
        except Exception as error:
            self.failed.emit(str(error) or type(error).__name__)


class UpdateDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        current_version: str = APP_VERSION,
        repo: str = GITHUB_REPO,
        api_base: str = GITHUB_API_BASE,
        allow_local_http: bool = False,
        install_target: InstallTarget | None = None,
    ):
        super().__init__(parent)
        self.current_version = current_version
        self.repo = repo
        self.api_base = api_base
        self.allow_local_http = allow_local_http
        self.install_target = install_target if install_target is not None else detect_install_target()
        self.release: ReleaseInfo | None = None
        self.staged_bundle: tuple[Path, Path] | None = None
        self._staging_dir: Path | None = None
        self._check_worker: _CheckWorker | None = None
        self._download_worker: _DownloadWorker | None = None

        self.setWindowTitle("检查更新")
        self.setMinimumWidth(470)
        self._build_ui()
        self.start_check()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 15, 16, 14)
        outer.setSpacing(10)

        self.headline = label("正在检查新版本…", "panelTitle")
        self.headline.setWordWrap(True)
        outer.addWidget(self.headline)

        self.subtitle = label(f"当前版本 v{self.current_version}", "meta")
        self.subtitle.setWordWrap(True)
        outer.addWidget(self.subtitle)

        self.notes_box = panel()
        notes_layout = QVBoxLayout(self.notes_box)
        notes_layout.setContentsMargins(10, 8, 10, 9)
        notes_layout.setSpacing(6)
        notes_layout.addWidget(label("更新说明", "sectionLabel"))
        self.notes_view = QPlainTextEdit()
        self.notes_view.setReadOnly(True)
        self.notes_view.setMinimumHeight(136)
        notes_layout.addWidget(self.notes_view)
        self.notes_box.hide()
        outer.addWidget(self.notes_box, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        outer.addWidget(self.progress_bar)

        self.status_label = label("正在连接 GitHub Releases…", "muted")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.open_page_button = button("打开发布页", "quiet")
        self.open_page_button.clicked.connect(self._open_release_page)
        self.retry_button = button("重试", "quiet")
        self.retry_button.clicked.connect(self.start_check)
        self.install_button = button("立即下载并更新", "accent")
        self.install_button.clicked.connect(self.start_download)
        self.close_button = button("取消", "quiet")
        self.close_button.clicked.connect(self.reject)

        actions.addWidget(self.open_page_button)
        actions.addWidget(self.retry_button)
        actions.addStretch()
        actions.addWidget(self.install_button)
        actions.addWidget(self.close_button)
        outer.addLayout(actions)

        self.open_page_button.hide()
        self.retry_button.hide()
        self.install_button.hide()

    def start_check(self) -> None:
        if self._check_worker and self._check_worker.isRunning():
            return
        self.release = None
        self.headline.setText("正在检查新版本…")
        self.subtitle.setText(f"当前版本 v{self.current_version}")
        self.notes_box.hide()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.status_label.setText("正在连接 GitHub Releases…")
        self.status_label.show()
        self.open_page_button.hide()
        self.retry_button.hide()
        self.install_button.hide()
        self.close_button.setText("取消")

        worker = _CheckWorker(self.repo, self.api_base, self.allow_local_http, self)
        worker.found.connect(self._on_check_found)
        worker.failed.connect(self._on_check_failed)
        self._check_worker = worker
        worker.start()

    def _on_check_found(self, release: ReleaseInfo) -> None:
        self.release = release
        self.progress_bar.hide()
        if not is_newer(release.version, self.current_version):
            self.headline.setText("当前已是最新版本")
            self.subtitle.setText(f"当前版本 v{self.current_version}（最新发布 v{release.version}）")
            self.status_label.setText("暂不需要更新。")
            self.open_page_button.show()
            self.close_button.setText("关闭")
            self.close_button.setFocus()
            return

        self.headline.setText(f"发现新版本 v{release.version}")
        details = [f"当前版本 v{self.current_version}"]
        if release.asset_size > 0:
            details.append(f"安装包约 {format_size(release.asset_size)}")
        if release.sha256:
            details.append("支持 SHA-256 校验")
        self.subtitle.setText("　·　".join(details))
        self.notes_view.setPlainText(release.notes or "该版本未填写更新说明。")
        self.notes_box.show()
        self.open_page_button.show()
        self.close_button.setText("稍后再说")

        if not release.asset_url:
            self.status_label.setText("该版本未附带 Windows 便携版 ZIP，可前往发布页手动下载。")
            self.install_button.hide()
        elif not self.install_target.writable:
            self.status_label.setText(self.install_target.reason)
            self.install_button.hide()
        else:
            self.status_label.setText("点击「立即下载并更新」后将自动校验、退出程序并重启进入新版本。")
            self.install_button.show()
            self.install_button.setFocus()

    def _on_check_failed(self, message: str) -> None:
        self.progress_bar.hide()
        self.headline.setText("检查更新失败")
        self.subtitle.setText(f"当前版本 v{self.current_version}")
        self.status_label.setText(message)
        self.open_page_button.show()
        self.retry_button.show()
        self.install_button.hide()
        self.close_button.setText("关闭")

    def start_download(self) -> None:
        if self.release is None or (self._download_worker and self._download_worker.isRunning()):
            return
        self._cleanup_staging()
        self._staging_dir = make_staging_dir()
        self.install_button.setEnabled(False)
        self.retry_button.hide()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.status_label.setText("正在准备下载…")
        self.close_button.setText("取消下载")

        worker = _DownloadWorker(self.release, self._staging_dir, self.allow_local_http, self)
        worker.progress.connect(self._on_download_progress)
        worker.phase.connect(self._on_download_phase)
        worker.ready.connect(self._on_download_ready)
        worker.cancelled.connect(self._on_download_cancelled)
        worker.failed.connect(self._on_download_failed)
        self._download_worker = worker
        worker.start()

    def _on_download_progress(self, done: int, total: int) -> None:
        if total > 0:
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
            percent = min(100, int(done * 100 / total))
            self.progress_bar.setValue(percent)
            self.status_label.setText(f"正在下载新版本… {format_size(done)} / {format_size(total)}")
        else:
            if self.progress_bar.maximum() != 0:
                self.progress_bar.setRange(0, 0)
            self.status_label.setText(f"正在下载新版本… 已下载 {format_size(done)}")

    def _on_download_phase(self, message: str) -> None:
        self.status_label.setText(message)
        if "校验" in message:
            self.progress_bar.setRange(0, 0)

    def _on_download_ready(self, bundle_root: Path, staging_dir: Path) -> None:
        self.staged_bundle = (bundle_root, staging_dir)
        self.accept()

    def _on_download_cancelled(self) -> None:
        self._cleanup_staging()
        self.progress_bar.hide()
        self.install_button.setEnabled(True)
        self.status_label.setText("已取消下载。")
        self.close_button.setText("关闭")

    def _on_download_failed(self, message: str) -> None:
        self._cleanup_staging()
        self.progress_bar.hide()
        self.install_button.setEnabled(True)
        self.install_button.setText("重新下载")
        self.status_label.setText(message)
        self.open_page_button.show()
        self.close_button.setText("关闭")

    def _open_release_page(self) -> None:
        target = self.release.html_url if self.release else RELEASES_PAGE_URL
        QDesktopServices.openUrl(QUrl(target))

    def _cleanup_staging(self) -> None:
        if self._staging_dir and self._staging_dir.exists():
            shutil.rmtree(self._staging_dir, ignore_errors=True)
        self._staging_dir = None

    def _stop_workers(self) -> None:
        if self._download_worker and self._download_worker.isRunning():
            self._download_worker.cancel_event.set()
            self._download_worker.wait(5000)
        if self._check_worker and self._check_worker.isRunning():
            self._check_worker.wait(5000)

    def reject(self) -> None:
        if self._download_worker and self._download_worker.isRunning():
            self._download_worker.cancel_event.set()
            self.close_button.setEnabled(False)
            self.status_label.setText("正在取消下载…")
            self._download_worker.wait(5000)
            self.close_button.setEnabled(True)
        self._stop_workers()
        self._cleanup_staging()
        super().reject()
