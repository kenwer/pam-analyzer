"""Validation and renaming behavior of FolderImportDialog's card-name table."""

from pathlib import Path

from PySide6.QtWidgets import QDialogButtonBox

from pam_analyzer.domain.audio_import import DetectedCard, ImportSource
from pam_analyzer.ui.dialogs.folder_import_dialog import FolderImportDialog


def _card(name: str) -> DetectedCard:
    return DetectedCard(name=name, mountpoint=Path("/data") / name, device="", source=ImportSource.FOLDER)


def _ok_button(dialog: FolderImportDialog):
    return dialog.ui.button_box.button(QDialogButtonBox.StandardButton.Ok)


def test_ok_enabled_for_valid_distinct_names(qtbot):
    dialog = FolderImportDialog([_card("CardA"), _card("CardB")], [3, 5])
    qtbot.addWidget(dialog)
    assert _ok_button(dialog).isEnabled()
    assert dialog.ui.validation_label.text() == ""


def test_empty_name_disables_ok(qtbot):
    dialog = FolderImportDialog([_card("CardA")], [3])
    qtbot.addWidget(dialog)
    dialog.ui.card_table.item(0, 0).setText("  ")
    assert not _ok_button(dialog).isEnabled()
    assert "Invalid" in dialog.ui.validation_label.text()


def test_slash_in_name_disables_ok(qtbot):
    dialog = FolderImportDialog([_card("CardA")], [3])
    qtbot.addWidget(dialog)
    dialog.ui.card_table.item(0, 0).setText("Card/A")
    assert not _ok_button(dialog).isEnabled()


def test_duplicate_names_disable_ok(qtbot):
    dialog = FolderImportDialog([_card("CardA"), _card("CardB")], [3, 5])
    qtbot.addWidget(dialog)
    dialog.ui.card_table.item(1, 0).setText("CardA")
    assert not _ok_button(dialog).isEnabled()
    assert "Duplicate" in dialog.ui.validation_label.text()


def test_fixing_a_duplicate_re_enables_ok(qtbot):
    dialog = FolderImportDialog([_card("CardA"), _card("CardB")], [3, 5])
    qtbot.addWidget(dialog)
    dialog.ui.card_table.item(1, 0).setText("CardA")
    assert not _ok_button(dialog).isEnabled()
    dialog.ui.card_table.item(1, 0).setText("CardC")
    assert _ok_button(dialog).isEnabled()


def test_result_cards_reflects_edited_names_in_order():
    dialog = FolderImportDialog([_card("CardA"), _card("CardB")], [3, 5])
    dialog.ui.card_table.item(0, 0).setText("Renamed")
    renamed = dialog.result_cards()
    assert [c.name for c in renamed] == ["Renamed", "CardB"]
    # Only the name changes; mountpoint/device/source carry over unchanged.
    assert renamed[0].mountpoint == Path("/data/CardA")
    assert renamed[0].source is ImportSource.FOLDER


def test_file_counts_shown_read_only(qtbot):
    dialog = FolderImportDialog([_card("CardA")], [7])
    qtbot.addWidget(dialog)
    files_item = dialog.ui.card_table.item(0, 1)
    assert files_item.text() == "7"
    from PySide6.QtCore import Qt

    assert not (files_item.flags() & Qt.ItemFlag.ItemIsEditable)


def test_clear_after_defaults_unchecked(qtbot):
    dialog = FolderImportDialog([_card("CardA")], [3])
    qtbot.addWidget(dialog)
    assert dialog.clear_after() is False


def test_clear_after_reflects_checkbox(qtbot):
    dialog = FolderImportDialog([_card("CardA")], [3])
    qtbot.addWidget(dialog)
    dialog.ui.clear_after_check.setChecked(True)
    assert dialog.clear_after() is True
