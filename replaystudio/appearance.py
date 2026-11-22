import sys
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

from .model import DEFAULT_FONT, ReplayError
from .widgets import STYLESHEET


DEFAULT_TONE = "green"
TONE_NAMES = {"green": "青绿", "blue": "冷蓝灰", "warm": "暖灰"}
TONE_KEY = "appearance/tone"
TONES = {
    "green": dict(
        background="#131c1e", panel="#192426", well="#10191b", field="#121d1f",
        list_base="#152022", list_alt="#182527", control="#253436", hover="#2d4243",
        header="#223033", line="#354749", text="#e6ece9", muted="#a5b8b5", dim="#8da19b",
        accent="#bce3c8", accent_text="#172e29", selection="#35544f", grip="#627f78",
    ),
    "blue": dict(
        background="#141a24", panel="#1d2532", well="#111822", field="#161f2c",
        list_base="#19222f", list_alt="#1d293a", control="#293548", hover="#30425b",
        header="#243144", line="#3b4a61", text="#e8eef8", muted="#b5c3d8", dim="#909fb7",
        accent="#bdd5ff", accent_text="#152741", selection="#3b5376", grip="#657f9f",
    ),
    "warm": dict(
        background="#201d1b", panel="#2a2521", well="#1a1715", field="#221c18",
        list_base="#251f1b", list_alt="#2a241f", control="#38302b", hover="#45382f",
        header="#352b25", line="#55483e", text="#f1e9e0", muted="#d0bdac", dim="#ad9a89",
        accent="#e8cca9", accent_text="#302319", selection="#604a39", grip="#90745f",
    ),
}


def theme_settings() -> QSettings:
    return QSettings("ReplayStudio", "ReplayStudio")


def apply_tone(app: QApplication, key: str, remember: bool = False) -> None:
    key = key if key in TONES else DEFAULT_TONE
    colors = TONES[key]
    palette = app.palette()
    for role, color in (
        ("Window", "background"), ("Base", "field"), ("AlternateBase", "list_alt"),
        ("Text", "text"), ("WindowText", "text"), ("Button", "control"), ("ButtonText", "text"),
        ("PlaceholderText", "muted"), ("Highlight", "selection"), ("HighlightedText", "text"),
        ("Accent", "accent"), ("Dark", "well"), ("Mid", "line"), ("Midlight", "header"),
        ("Light", "hover"), ("Shadow", "grip"),
    ):
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(colors[color]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(colors["dim"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(colors["dim"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(colors["dim"]))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET.substitute(colors))
    app.setProperty("replayTone", key)
    if remember:
        theme_settings().setValue(TONE_KEY, key)


def configure_appearance(app: QApplication) -> None:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    typeface = root / "resources" / "NotoSansSC.ttf"
    if QFontDatabase.addApplicationFont(str(typeface)) < 0:
        raise ReplayError("缺少画面所用的字体。请完整解压便携版文件夹。")
    app.setStyle("Fusion")
    app.setFont(QFont(DEFAULT_FONT, 10))
    apply_tone(app, theme_settings().value(TONE_KEY, DEFAULT_TONE, type=str))
