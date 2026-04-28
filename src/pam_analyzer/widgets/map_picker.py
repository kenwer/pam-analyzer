import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QVBoxLayout, QWidget

_ZOOM_KEY = "map/zoom_level"
_DEFAULT_ZOOM = 10.0


def _resolve_qml_path() -> Path:
    """Return the filesystem path to *map_picker.qml*.

    When running from a PyInstaller one-file bundle the QML file is extracted
    to the ``widgets/`` sub-directory inside ``sys._MEIPASS``.  When running
    directly from source it sits next to this module.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "widgets" / "map_picker.qml"
    return Path(__file__).with_name("map_picker.qml")


class MapPickerWidget(QWidget):
    locationPicked = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = QSettings()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._qw = QQuickWidget(self)
        self._qw.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        qml_path = _resolve_qml_path()
        self._qw.setSource(QUrl.fromLocalFile(str(qml_path)))

        layout.addWidget(self._qw)

        root = self._qw.rootObject()
        if root:
            root.locationPicked.connect(self._on_location_picked)
            root.zoomChanged.connect(self._on_zoom_changed)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        # QQuickWidget inside QStackedWidget doesn't repaint on first show;
        # a deferred update forces the scene graph to render once the event
        # loop processes the visibility change.
        QTimer.singleShot(0, self._qw.update)

    @Slot(float, float)
    def _on_location_picked(self, lat: float, lon: float) -> None:
        self.locationPicked.emit(lat, lon)

    @Slot(float)
    def _on_zoom_changed(self, zoom: float) -> None:
        self._settings.setValue(_ZOOM_KEY, zoom)

    def _saved_zoom(self) -> float:
        return float(self._settings.value(_ZOOM_KEY, _DEFAULT_ZOOM))  # type: ignore[arg-type]

    def set_location(self, lat: float, lon: float) -> None:
        root = self._qw.rootObject()
        if root:
            root.setMarker(lat, lon, self._saved_zoom())  # type: ignore[attr-defined]

    def clear(self) -> None:
        root = self._qw.rootObject()
        if root:
            root.clearMarker()  # type: ignore[attr-defined]
