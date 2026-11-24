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


def test_reentrant_show_builds_one_view(qtbot, monkeypatch):
    """Parenting the view to a shown window makes Qt recreate the native
    window, which re-enters showEvent. Offscreen never does, so simulate it.
    """
    widget = MapPickerWidget()
    qtbot.addWidget(widget)
    add_widget = widget._layout.addWidget

    def add_and_reshow(child):
        monkeypatch.undo()  # Qt recreates the native window only once
        add_widget(child)
        widget.hide()
        widget.show()

    monkeypatch.setattr(widget._layout, "addWidget", add_and_reshow)

    widget.show()

    assert len(widget.findChildren(QQuickWidget)) == 1
