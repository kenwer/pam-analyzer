"""About dialog: app header plus Changelog and Acknowledgements tabs.

Stateless wrapper applied to a plain QDialog (mirrors the detectorist pattern).
"""

from importlib.metadata import PackageNotFoundError, version

from PySide6.QtCore import QFile, QIODeviceBase, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QDialog, QWidget

from .. import resources_rc  # noqa: F401  registers :/icons/* and :/docs/* resources
from .ui_about_dialog import Ui_AboutDialog

# Keep in sync with the "Acknowledgements" section of README.md.
_ACKNOWLEDGEMENTS_MARKDOWN = """\
The author would like to thank the following projects:

- [BirdNET](https://github.com/birdnet-team/birdnet)
- [Perch 2.0](https://arxiv.org/pdf/2508.04665)
- [Qt](https://www.qt.io/) / [PySide6](https://doc.qt.io/qtforpython/)
"""


def show_about_dialog(parent: QWidget | None = None) -> None:
    dialog = QDialog(parent)
    ui = Ui_AboutDialog()
    ui.setupUi(dialog)

    ui.version_label.setText(f"Version: {_resolve_version()}")
    ui.icon_label.setPixmap(QIcon(":/icons/icon.svg").pixmap(QSize(128, 128)))

    ui.changelog_text_browser.setMarkdown(_read_qresource(":/docs/CHANGELOG.md"))
    ui.acknowledgements_text_browser.setMarkdown(_ACKNOWLEDGEMENTS_MARKDOWN)

    dialog.exec()


def _resolve_version() -> str:
    try:
        return version("pam-analyzer")
    except PackageNotFoundError:
        return "unknown"


def _read_qresource(path: str) -> str:
    # QTextBrowser.source only renders HTML, so we read the markdown text
    # ourselves and hand it to setMarkdown(). Same trick detectorist uses.
    qfile = QFile(path)
    if not qfile.open(QIODeviceBase.OpenModeFlag.ReadOnly | QIODeviceBase.OpenModeFlag.Text):
        return ""
    try:
        return bytes(qfile.readAll()).decode("utf-8")
    finally:
        qfile.close()
