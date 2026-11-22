import logging
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from replaystudio.appearance import configure_appearance
from replaystudio.model import ReplayError
from replaystudio.window import MainWindow


def _setup_logging() -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ReplayStudio"
    try:
        base.mkdir(parents=True, exist_ok=True)
        log_file = base / "replay.log"
        if log_file.is_file() and log_file.stat().st_size > 512 * 1024:
            backup = base / "replay.log.old"
            backup.unlink(missing_ok=True)
            log_file.replace(backup)
        logging.basicConfig(
            filename=str(log_file),
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            encoding="utf-8",
        )
        return log_file
    except OSError:
        return None


def main() -> int:
    _setup_logging()
    logging.info("ReplayStudio starting (frozen=%s)", getattr(sys, "frozen", False))

    def _handle_exception(exc_type, exc_value, exc_tb):
        logging.error("Unhandled exception:\n%s", "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _handle_exception

    app = QApplication(sys.argv)
    app.setApplicationName("回放工坊")
    try:
        configure_appearance(app)
    except ReplayError as error:
        logging.error("Appearance init failed: %s", error)
        QMessageBox.critical(None, "回放工坊启动失败", str(error))
        return 1
    editor = MainWindow()
    editor.show()
    if len(sys.argv) > 1:
        editor.load_path(Path(sys.argv[1]).resolve())
    code = app.exec()
    logging.info("ReplayStudio exited with code %s", code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
