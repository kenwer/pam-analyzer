"""Download status of the engine selected for a run."""

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QWidget

_MB = 1024 * 1024


def to_mb(n_bytes: int) -> int:
    return math.ceil(n_bytes / _MB)


class ModelStatusWidget(QWidget):
    """Download state of one engine, placed next to the Run button it affects."""

    downloadRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: palette(placeholder-text);")
        self.action_label = QLabel()
        self.action_label.setTextFormat(Qt.TextFormat.RichText)
        self.action_label.linkActivated.connect(self._on_link)
        self.action_label.hide()
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(6)
        self.progress_bar.setFixedWidth(120)
        self.progress_bar.hide()

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.status_label)
        row.addWidget(self.progress_bar)
        row.addWidget(self.action_label)
        row.addStretch()

    def show_model(self, missing_bytes: int) -> None:
        if missing_bytes > 0:
            self.status_label.setText(f"Model not downloaded ({to_mb(missing_bytes)} MB)")
            self._set_action("download", "Download now")
        else:
            self.status_label.clear()  # an installed model needs no mention
            self._set_action(None)
        self.progress_bar.hide()

    def show_download(self, done: int, total: int) -> None:
        """done and total count bytes."""
        done_mb, total_mb = to_mb(done), to_mb(total)
        # No total yet: queued behind another engine's install
        self.status_label.setText(f"Downloading model {done_mb} / {total_mb} MB" if total else "Waiting to download")
        self._set_action("cancel", "Cancel")
        self.progress_bar.setRange(0, max(total_mb, 1))
        self.progress_bar.setValue(min(done_mb, total_mb))
        self.progress_bar.show()

    def _set_action(self, href: str | None, text: str = "") -> None:
        self.action_label.setVisible(href is not None)
        if href is not None:
            self.action_label.setText(f'<a href="{href}">{text}</a>')

    def _on_link(self, href: str) -> None:
        (self.downloadRequested if href == "download" else self.cancelRequested).emit()
