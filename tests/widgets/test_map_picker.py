"""MapPickerWidget under the test suite's map stub."""

from PySide6.QtQuickWidgets import QQuickWidget

from pam_analyzer.widgets import MapPickerWidget


def test_map_view_is_built_on_first_show(qtbot):
    """Building it at construction would load Qt Quick during app startup."""
    widget = MapPickerWidget()
    qtbot.addWidget(widget)
    widget.set_location(48.0, 11.0)
    assert widget.findChild(QQuickWidget) is None

    widget.show()

    assert widget.findChild(QQuickWidget) is not None


def test_no_map_is_loaded_under_test(qtbot):
    """Guards the _no_live_map fixture: a loaded map would fetch real tiles."""
    widget = MapPickerWidget()
    qtbot.addWidget(widget)
    widget.show()

    view = widget.findChild(QQuickWidget)
    assert view is not None
    assert view.rootObject() is None
    widget.set_location(48.0, 11.0)
    widget.clear()
