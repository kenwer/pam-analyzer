import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from PySide6.QtCore import QSettings, QTimer, QUrl, Signal, SignalInstance, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtNetwork import QSslSocket
from PySide6.QtWidgets import QVBoxLayout, QWidget

if TYPE_CHECKING:
    from PySide6.QtQuickWidgets import QQuickWidget


class _MapRoot(Protocol):
    """The signals map_picker.qml declares on its root item."""

    locationPicked: SignalInstance
    zoomChanged: SignalInstance


_ZOOM_KEY = "map/zoom_level"
_DEFAULT_ZOOM = 10.0

# Qt prefers OpenSSL whenever it finds the DLLs on PATH. A stray copy there
# (Git for Windows ships one) crashes the tile fetch, so use Windows' own TLS.
if sys.platform == "win32":
    QSslSocket.setActiveBackend("schannel")


def _resolve_qml_path() -> Path:
    """Return the filesystem path to *map_picker.qml*.

    The compiled build bundles the file at this package's path, so __file__
    resolves next to it there as well as in a source run.
    """
    return Path(__file__).with_name("map_picker.qml")


class MapPickerWidget(QWidget):
    """An OpenStreetMap view for picking a location.

    The QML view is built on first show, as loading Qt Quick and the QML
    modules costs seconds on a cold Windows start. Until then set_location()
    and clear() only record the marker that the view shows once it exists.
    """

    locationPicked = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = QSettings()
        self._qw: QQuickWidget | None = None
        self._marker: tuple[float, float] | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

    def _build_view(self) -> QQuickWidget:
        from PySide6.QtQuickWidgets import QQuickWidget

        qw = QQuickWidget()  # No parent yet, see showEvent()
        qw.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        qw.setSource(QUrl.fromLocalFile(str(_resolve_qml_path())))
        root = cast("_MapRoot | None", qw.rootObject())
        if root:
            root.locationPicked.connect(self._on_location_picked)
            root.zoomChanged.connect(self._on_zoom_changed)
        return qw

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._qw is None:
            # Parenting the first QQuickWidget to a shown window recreates the
            # native window and re-enters showEvent(), so set _qw before addWidget().
            self._qw = self._build_view()
            self._layout.addWidget(self._qw)
            # A child added to a visible parent stays hidden until shown
            self._qw.show()
            self._apply_marker()
        # QQuickWidget inside QStackedWidget doesn't repaint on first show;
        # a deferred update forces the scene graph to render once the event
        # loop processes the visibility change.
        QTimer.singleShot(0, self._qw, self._qw.update)

    @Slot(float, float)
    def _on_location_picked(self, lat: float, lon: float) -> None:
        self.locationPicked.emit(lat, lon)

    @Slot(float)
    def _on_zoom_changed(self, zoom: float) -> None:
        self._settings.setValue(_ZOOM_KEY, zoom)

    def _saved_zoom(self) -> float:
        return float(self._settings.value(_ZOOM_KEY, _DEFAULT_ZOOM))  # type: ignore[arg-type]

    def set_location(self, lat: float, lon: float) -> None:
        self._marker = (lat, lon)
        self._apply_marker()

    def clear(self) -> None:
        self._marker = None
        self._apply_marker()

    def _apply_marker(self) -> None:
        root = self._qw.rootObject() if self._qw is not None else None
        if not root:
            return
        if self._marker is None:
            root.clearMarker()  # type: ignore[attr-defined]
        else:
            root.setMarker(*self._marker, self._saved_zoom())  # type: ignore[attr-defined]
