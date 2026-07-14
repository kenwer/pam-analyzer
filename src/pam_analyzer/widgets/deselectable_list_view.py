"""A QListView that clears its selection when the user clicks empty space."""

from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QListView


class DeselectableListView(QListView):
    """QListView that deselects when a press lands outside every item.

    Qt item views keep their selection when the user clicks the empty area
    below the rows (there is no property to change this), so the file-manager
    style "click the background to deselect" is left to the application. This
    view adds it: a press on an invalid index clears the selection before the
    base class runs its normal handling.
    """

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.indexAt(event.position().toPoint()).isValid():
            self.clearSelection()
        super().mousePressEvent(event)
