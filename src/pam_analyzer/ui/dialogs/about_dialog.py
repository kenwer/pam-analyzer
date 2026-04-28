"""About dialog: app icon, version, author, link, short description.

Stateless wrapper applied to a plain QDialog (mirrors the detectorist pattern).
"""

from importlib.metadata import PackageNotFoundError, version

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QDialog, QWidget

from .. import resources_rc  # noqa: F401  registers :/icons/* resources
from .ui_about_dialog import Ui_AboutDialog

_DESCRIPTION_HTML = """
<p>PAM Analyzer is a desktop tool for reviewing
<b>BirdNET</b> detections from passive acoustic monitoring (PAM) field
recordings. It supports importing audio from SD cards, running BirdNET
analyses, and curating species detections per campaign.</p>
<p>This is a clean Qt rewrite of the original NiceGUI-based app.</p>
"""


def show_about_dialog(parent: QWidget | None = None) -> None:
    dialog = QDialog(parent)
    ui = Ui_AboutDialog()
    ui.setupUi(dialog)

    ui.version_label.setText(f"Version: {_resolve_version()}")
    ui.icon_label.setPixmap(QIcon(":/icons/icon.svg").pixmap(QSize(128, 128)))
    ui.description_text_browser.setHtml(_DESCRIPTION_HTML)

    dialog.exec()


def _resolve_version() -> str:
    try:
        return version("pam-analyzer")
    except PackageNotFoundError:
        return "unknown"
