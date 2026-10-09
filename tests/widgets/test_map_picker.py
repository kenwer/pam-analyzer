"""MapPickerWidget under the test suite's map stub."""

from pam_analyzer.widgets import MapPickerWidget


def test_no_map_is_loaded_under_test(qtbot):
    """Guards the _no_live_map fixture: a loaded map would fetch real tiles."""
    widget = MapPickerWidget()
    qtbot.addWidget(widget)

    assert widget._qw.rootObject() is None
    widget.set_location(48.0, 11.0)
    widget.clear()
