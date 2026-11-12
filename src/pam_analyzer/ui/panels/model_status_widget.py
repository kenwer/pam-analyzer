"""Download status of the engine selected for a run."""

import math

from PySide6.QtCore import Signal, SignalInstance
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QWidget

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
        self.action_button = QPushButton()
        self.action_button.clicked.connect(self._on_action)
        self.action_button.hide()
        self._action: SignalInstance | None = None
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(6)
        self.progress_bar.setFixedWidth(120)
        self.progress_bar.hide()

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.status_label)
        row.addWidget(self.progress_bar)
        row.addWidget(self.action_button)
        row.addStretch()

    def show_model(self, missing_bytes: int) -> None:
        if missing_bytes > 0:
            self.status_label.setText(f"Model not downloaded ({to_mb(missing_bytes)} MB)")
            self._set_action(self.downloadRequested, "Download now")
        else:
            self.status_label.clear()  # an installed model needs no mention
            self._set_action(None)
        self.progress_bar.hide()

    def show_download(self, done: int, total: int) -> None:
        """done and total count bytes."""
        done_mb, total_mb = to_mb(done), to_mb(total)
        # No total yet: queued behind another engine's install
        self.status_label.setText(f"Downloading model {done_mb} / {total_mb} MB" if total else "Waiting to download")
        self._set_action(self.cancelRequested, "Cancel")
        self.progress_bar.setRange(0, max(total_mb, 1))
        self.progress_bar.setValue(min(done_mb, total_mb))
        self.progress_bar.show()

    def _set_action(self, signal: SignalInstance | None, text: str = "") -> None:
        self._action = signal
        self.action_button.setText(text)
        self.action_button.setVisible(signal is not None)

    def _on_action(self) -> None:
        if self._action is not None:
            self._action.emit()
