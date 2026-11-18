"""Start/stop button whose checked state only its owner sets.

A plain checkable QPushButton flips its checked state on every click, before
the clicked handler can decide whether the start actually happens. Here a
click only emits clicked, and setChecked() is the one way to change the
state, so the red checked style always shows the owner's state.
"""

from PySide6.QtWidgets import QPushButton, QWidget

_STYLE = """
QPushButton {
  background-color: #5a8dee;
  color: white;
  border: none;
  border-radius: 4px;
  padding: 6px 14px;
  font-weight: 600;
}
QPushButton:hover { background-color: #4a7dd0; }
QPushButton:pressed { background-color: #3d6cb8; }
QPushButton:checked { background-color: #e53935; }
QPushButton:checked:hover { background-color: #d32f2f; }
QPushButton:checked:pressed { background-color: #c62828; }
QPushButton:disabled { background-color: palette(button); color: palette(placeholder-text); }
"""


class StateButton(QPushButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setStyleSheet(_STYLE)

    def nextCheckState(self) -> None:  # noqa: N802 (Qt API)
        pass
