"""Unit tests for the filter editor popups (canonical text in and out)."""

from PySide6.QtCore import QDate, Qt, QTime

from pam_analyzer.domain.filter_ops import FilterOp
from pam_analyzer.widgets.filter_popups import (
    POPUP_OPS,
    DateRangePopup,
    SetPopup,
    SingleDatePopup,
    TimeRangePopup,
    create_popup,
)


def _applied(qtbot, popup) -> str:
    with qtbot.waitSignal(popup.applyRequested) as blocker:
        popup._apply()
    return blocker.args[0]


# Date popups


def test_single_date_popup_prefills_and_emits(qtbot):
    popup = SingleDatePopup("2026-04-25")
    qtbot.addWidget(popup)
    assert popup._date.date() == QDate(2026, 4, 25)
    assert _applied(qtbot, popup) == "2026-04-25"


def test_single_date_popup_defaults_to_today_on_garbage(qtbot):
    popup = SingleDatePopup("not a date")
    qtbot.addWidget(popup)
    assert popup._date.date() == QDate.currentDate()


def test_date_range_popup_roundtrip(qtbot):
    popup = DateRangePopup("2026-04-25 .. 2026-05-02")
    qtbot.addWidget(popup)
    assert popup._from.date() == QDate(2026, 4, 25)
    assert popup._to.date() == QDate(2026, 5, 2)
    assert _applied(qtbot, popup) == "2026-04-25 .. 2026-05-02"


def test_date_range_popup_normalizes_reversed_prefill(qtbot):
    popup = DateRangePopup("2026-05-02 .. 2026-04-25")
    qtbot.addWidget(popup)
    assert popup._from.date() == QDate(2026, 4, 25)
    assert popup._to.date() == QDate(2026, 5, 2)


# Time popup


def test_time_range_popup_roundtrip(qtbot):
    popup = TimeRangePopup("04:00 - 08:00")
    qtbot.addWidget(popup)
    assert popup._from.time() == QTime(4, 0)
    assert popup._to.time() == QTime(8, 0)
    assert _applied(qtbot, popup) == "04:00 - 08:00"


def test_time_range_popup_keeps_overnight_order(qtbot):
    """Start after end means a window wrapping midnight; it must not swap."""
    popup = TimeRangePopup("22:00 - 04:00")
    qtbot.addWidget(popup)
    assert _applied(qtbot, popup) == "22:00 - 04:00"


def test_time_range_popup_defaults_to_full_day(qtbot):
    popup = TimeRangePopup("")
    qtbot.addWidget(popup)
    assert _applied(qtbot, popup) == "00:00 - 23:59"


# Set popup


def test_set_popup_prechecks_from_current_text(qtbot):
    popup = SetPopup(["MSD-1", "MSD-2", "MSD-3"], "msd-1; MSD-3")
    qtbot.addWidget(popup)
    assert popup.checked_values() == ["MSD-1", "MSD-3"]


def test_set_popup_emits_checked_values_in_list_order(qtbot):
    popup = SetPopup(["A", "B", "C"], "")
    qtbot.addWidget(popup)
    popup._list.item(2).setCheckState(Qt.CheckState.Checked)
    popup._list.item(0).setCheckState(Qt.CheckState.Checked)
    assert _applied(qtbot, popup) == "A; C"


def test_set_popup_search_hides_and_all_respects_filter(qtbot):
    popup = SetPopup(["Robin", "Blackbird", "Rook"], "")
    qtbot.addWidget(popup)
    popup._search.setText("ro")
    hidden = [popup._list.item(i).isHidden() for i in range(3)]
    assert hidden == [False, True, False]
    # "All" only checks the visible items.
    popup._set_visible_checked(True)
    assert popup.checked_values() == ["Robin", "Rook"]
    # Clearing the search reveals everything again, checks untouched.
    popup._search.setText("")
    assert [popup._list.item(i).isHidden() for i in range(3)] == [False, False, False]
    assert popup.checked_values() == ["Robin", "Rook"]


def test_set_popup_none_unchecks_visible(qtbot):
    popup = SetPopup(["A", "B"], "A; B")
    qtbot.addWidget(popup)
    popup._set_visible_checked(False)
    assert popup.checked_values() == []


# Factory


def test_create_popup_maps_every_popup_op(qtbot):
    expected = {
        FilterOp.ON_DATE: SingleDatePopup,
        FilterOp.BEFORE_DATE: SingleDatePopup,
        FilterOp.AFTER_DATE: SingleDatePopup,
        FilterOp.DATE_RANGE: DateRangePopup,
        FilterOp.TIME_OF_DAY_RANGE: TimeRangePopup,
        FilterOp.IS_ANY_OF: SetPopup,
    }
    assert set(expected) == set(POPUP_OPS)
    for op, popup_type in expected.items():
        popup = create_popup(op, "", lambda: ["a"])
        assert isinstance(popup, popup_type)


def test_create_popup_returns_none_for_plain_ops(qtbot):
    assert create_popup(FilterOp.CONTAINS, "") is None
