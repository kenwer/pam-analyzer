"""Read text files embedded via resources.qrc."""

from PySide6.QtCore import QFile, QIODeviceBase

from . import resources_rc  # noqa: F401  registers :/icons/* and :/docs/* resources


def read_qresource_text(path: str) -> str:
    # QTextBrowser.source only renders HTML, so we read the markdown text
    # ourselves and hand it to setMarkdown(). Same trick detectorist uses.
    qfile = QFile(path)
    if not qfile.open(QIODeviceBase.OpenModeFlag.ReadOnly | QIODeviceBase.OpenModeFlag.Text):
        return ""
    try:
        return bytes(qfile.readAll()).decode("utf-8")
    finally:
        qfile.close()
