"""About dialog: app header plus Changelog and Acknowledgements tabs.

Stateless wrapper applied to a plain QDialog (mirrors the detectorist pattern).
"""

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QDialog, QWidget

from ... import __version__
from ..links import link
from ..qresource import read_qresource_text
from .ui_about_dialog import Ui_AboutDialog

# Keep in sync with the "Acknowledgements" section of README.md.
_ACKNOWLEDGEMENTS_MARKDOWN = """\
Many thanks to the people behind the projects PAM Analyzer builds on:

- [BirdNET](https://github.com/birdnet-team/birdnet) / [v2.4](https://zenodo.org/records/15050749) [v3.0](https://zenodo.org/records/24362980) / [BirdNET+ Taxonomy](https://github.com/birdnet-team/birdnet-taxonomy) (CC BY-SA 4.0)
- [Perch 2.0](https://arxiv.org/pdf/2508.04665) / [Perch-onnx](https://huggingface.co/justinchuby/Perch-onnx)
- [ONNX Runtime](https://onnxruntime.ai)
- [ONNX](https://onnx.ai) / [h5py](https://www.h5py.org)
- [Qt](https://www.qt.io/) / [PySide6](https://doc.qt.io/qtforpython)
- [Python](https://www.python.org)
- [Polars](https://pola.rs)
- [SciPy](https://scipy.org)
- [GUANO](https://github.com/riggsd/guano-py)
- [Mutagen](https://github.com/quodlibet/mutagen)
- [NumPy](https://numpy.org)
- [platformdirs](https://github.com/tox-dev/platformdirs)
- [soundfile](https://github.com/bastibe/python-soundfile)
- [libsndfile](https://libsndfile.github.io/libsndfile/) / [FLAC](https://xiph.org/flac/)
- [psutil](https://github.com/giampaolo/psutil)
- [pyqt-toast-notification](https://github.com/niklashenning/pyqt-toast-notification)
- [tomli-w](https://github.com/hukkin/tomli-w)
- [OpenStreetMap](https://www.openstreetmap.org)
- [Nuitka](https://nuitka.net)
- [uv](https://docs.astral.sh/uv/)
"""


def show_about_dialog(parent: QWidget | None = None) -> None:
    dialog = QDialog(parent)
    ui = Ui_AboutDialog()
    ui.setupUi(dialog)

    ui.version_label.setText(f"Version: {__version__}")
    ui.link_label.setText(link(ui.link_label.text(), ui.link_label.text()))
    ui.icon_label.setPixmap(QIcon(":/icons/icon.svg").pixmap(QSize(128, 128)))

    ui.changelog_text_browser.setMarkdown(read_qresource_text(":/docs/CHANGELOG.md"))
    ui.acknowledgements_text_browser.setMarkdown(_ACKNOWLEDGEMENTS_MARKDOWN)

    dialog.exec()
