"""Custom widgets must take their colours from the palette, not hardcode them.

A stylesheet that names a colour wins over the palette, so a widget styled that
way keeps the same background when the system switches to a dark theme. Each
test builds the widget once under a light palette and once under a dark one and
compares the pixels: a widget that paints the same colour both times is
ignoring the theme.

Comparing two renders rather than asserting one is dark is deliberate. How dark
a given widget goes is up to the active QStyle, and the offscreen platform used
in CI does not run the style the shipped macOS build does. Whether a widget
responds to the palette at all does not depend on the style.
"""

import pytest
from PySide6.QtGui import QColor, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QLineEdit, QPushButton

from pam_analyzer.widgets.audio_player import AudioPlayerPanel
from pam_analyzer.widgets.header_filter_row import HeaderFilterRow
from pam_analyzer.widgets.multi_column_sort_table import MultiColumnSortTable


def _palette(background: QColor, text: QColor) -> QPalette:
    """Every role set, so no role falls back to a light default."""
    palette = QPalette()
    for role in QPalette.ColorRole:
        if role != QPalette.ColorRole.NColorRoles:
            palette.setColor(role, background)
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.BrightText,
    ):
        palette.setColor(role, text)
    return palette


LIGHT = _palette(QColor(245, 245, 245), QColor(20, 20, 20))
DARK = _palette(QColor(30, 30, 30), QColor(230, 230, 230))


def _centre_colour(widget) -> QColor:
    """Colour at the centre of what the widget actually renders.

    The size check is load-bearing. A widget that never got a width grabs to an
    empty image, and reading a pixel out of range returns an arbitrary value
    that would let any comparison below pass without rendering anything.
    """
    image = widget.grab().toImage()
    assert image.width() and image.height(), (
        f"{type(widget).__name__} grabbed to an empty {image.width()}x{image.height()} "
        f"image, so this test would prove nothing"
    )
    return QColor(image.pixel(image.width() // 2, image.height() // 2))


@pytest.fixture
def theme(qapp, qtbot):
    """Build a widget under each palette and report the colour it painted.

    The widget has to be built after the palette is set. Changing the
    application palette does not repaint a widget that already exists, so
    reusing one would compare a render against itself.

    Takes a callable returning (root, {label: widget_to_sample}) and returns
    {label: (light_colour, dark_colour)}.
    """
    original = qapp.palette()

    def _both(build):
        painted: dict[str, list[QColor]] = {}
        for index, palette in enumerate((LIGHT, DARK)):
            qapp.setPalette(palette)
            root, targets = build()
            qtbot.addWidget(root)
            for label, widget in targets.items():
                painted.setdefault(label, [None, None])[index] = _centre_colour(widget)
        return {label: tuple(pair) for label, pair in painted.items()}

    yield _both

    qapp.setPalette(original)


def _build_line_edit(style: str = ""):
    edit = QLineEdit()
    if style:
        edit.setStyleSheet(style)
    edit.resize(240, 70)
    return edit, {"edit": edit}


def _build_player():
    panel = AudioPlayerPanel()
    panel.resize(240, 70)
    targets = {"panel": panel}
    for button in panel.findChildren(QPushButton):
        button.resize(32, 24)
        targets[f"button {button.text()!r}"] = button
    return panel, targets


def _build_filter_cell(qapp):
    """The row sizes each cell to its header section, so the table needs a
    model and a shown geometry or every cell comes out zero pixels wide."""
    model = QStandardItemModel(2, 2)
    model.setHorizontalHeaderLabels(["Species", "Confidence"])
    for row in range(2):
        for col in range(2):
            model.setItem(row, col, QStandardItem(f"r{row}c{col}"))

    table = MultiColumnSortTable()
    table.setSourceModel(model)
    table.resize(600, 200)
    table.show()
    qapp.processEvents()

    filter_row = HeaderFilterRow(table)
    filter_row.rebuild(2)
    qapp.processEvents()
    return table, {"filter cell": filter_row._slots[0].edit}


def test_an_unstyled_line_edit_responds_to_the_palette(theme):
    """Control. If this fails the harness is wrong and the rest proves nothing."""
    light, dark = theme(_build_line_edit)["edit"]
    assert light != dark


def test_a_hardcoded_background_is_detected(theme):
    """Control the other way: the defect this suite exists to catch is visible."""
    painted = theme(lambda: _build_line_edit("QLineEdit { background: white; }"))
    light, dark = painted["edit"]
    assert light == dark, "the comparison cannot see a hardcoded colour"


def test_the_player_panel_and_its_buttons_respond_to_the_palette(theme):
    painted = theme(_build_player)
    assert len(painted) >= 3, f"expected the panel and its buttons, got {list(painted)}"
    for label, (light, dark) in painted.items():
        assert light != dark, f"{label} paints {light.name()} under both palettes"


def test_a_filter_cell_responds_to_the_palette(theme, qapp):
    light, dark = theme(lambda: _build_filter_cell(qapp))["filter cell"]
    assert light != dark, f"filter cell paints {light.name()} under both palettes"
