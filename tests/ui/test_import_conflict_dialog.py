"""ImportConflictDialog never overwrites an imported file unless the user asks."""

from pam_analyzer.domain.audio_import import ConflictChoice, FileConflict
from pam_analyzer.ui.dialogs.import_conflict_dialog import ImportConflictDialog


def _conflict(name: str) -> FileConflict:
    return FileConflict(filename=name, src_size=2_000_000, src_mtime=0.0, dst_size=1_000_000, dst_mtime=0.0)


def test_every_conflict_defaults_to_skip_until_replace_all(qtbot) -> None:
    dialog = ImportConflictDialog([_conflict("a.flac"), _conflict("b.flac")])
    qtbot.addWidget(dialog)
    assert dialog.result_resolutions() == {"a.flac": ConflictChoice.SKIP, "b.flac": ConflictChoice.SKIP}

    dialog.ui.replace_all_button.click()
    assert set(dialog.result_resolutions().values()) == {ConflictChoice.REPLACE}

    dialog.ui.skip_all_button.click()
    assert set(dialog.result_resolutions().values()) == {ConflictChoice.SKIP}
