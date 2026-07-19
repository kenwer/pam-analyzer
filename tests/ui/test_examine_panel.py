"""pytest-qt smoke tests for the Examine panel."""

import csv
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QPoint, Qt
from PySide6.QtWidgets import QTabWidget, QWidget

from pam_analyzer.domain import Campaign, FilterMode, LatLon, Project
from pam_analyzer.domain.filter_ops import FilterOp
from pam_analyzer.infrastructure import (
    CsvDetectionRepository,
    SoundfileAudioExtractor,
    TomlCampaignRepository,
    TomlProjectRepository,
)
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.models.detections_table_model import COLUMNS_BY_NAME
from pam_analyzer.ui.panels.examine_panel import ExaminePanel
from pam_analyzer.ui.settings import AppSettings

_HEADERS = [
    "Campaign",
    "ARU",
    "Week",
    "Species",
    "Scientific_Name",
    "Confidence",
    "Start_Time",
    "End_Time",
    "Rank",
    "File",
    "Recording_Time",
    "Verified",
    "Corrected_Species",
    "Comment",
]


@pytest.fixture
def project(tmp_path: Path) -> Project:
    project_folder = tmp_path / "proj"
    project_folder.mkdir()

    cam_repo = TomlCampaignRepository()
    for name, aru in (("alpha", "MSD-1"), ("beta", "MSD-2")):
        folder = project_folder / name
        folder.mkdir()
        cam_repo.save(
            Campaign(
                name=name,
                folder=folder,
                species_filter_mode=FilterMode.LOCATION,
                location=LatLon(48.0, 11.0),
            )
        )
        csv_path = folder / "detections-BirdNET-2.4.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(_HEADERS)
            for i in range(3):
                w.writerow(
                    [
                        name,
                        aru,
                        "24",
                        "Robin",
                        "Erithacus rubecula",
                        f"{0.5 + i * 0.1}",
                        f"{i * 3.0}",
                        f"{i * 3.0 + 3.0}",
                        str(i + 1),
                        "f.wav",
                        # Distinct date and time-of-day per row so the
                        # date/time filter tests can slice the rows.
                        f"2026-04-{25 + i:02d}T{4 + i:02d}:00:00",
                        "",
                        "",
                        "",
                    ]
                )

    proj = Project(folder=project_folder)
    TomlProjectRepository().save(proj)
    return proj


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path, monkeypatch):
    """Route QSettings to a per-test scratch directory so AppSettings reads
    don't leak between tests or pollute the developer's real config."""
    from PySide6.QtCore import QCoreApplication, QSettings

    from pam_analyzer.ui.settings import AppSettings

    QCoreApplication.setOrganizationName("PAMAnalyzerTest")
    QCoreApplication.setApplicationName(f"PAMAnalyzerTest-{tmp_path.name}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "qsettings"))
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "qsettings"),
    )
    # AppSettings uses the QSettings(organization, application) constructor,
    # which Qt hardcodes to NativeFormat (the real CFPreferences store on
    # macOS) regardless of setDefaultFormat()/setPath() above. Redirect it
    # separately via an explicit file-backed QSettings so tests can never
    # write to the developer's actual application preferences.
    ini_path = tmp_path / "qsettings" / "app_settings.ini"
    monkeypatch.setattr(
        AppSettings,
        "__init__",
        lambda self: setattr(self, "_settings", QSettings(str(ini_path), QSettings.Format.IniFormat)),
    )
    yield


@pytest.fixture
def panel(qtbot, project: Project) -> ExaminePanel:
    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    panel = ExaminePanel(state, CsvDetectionRepository(), AppSettings(), SoundfileAudioExtractor())
    qtbot.addWidget(panel)
    state.load_project(project.folder)
    return panel


def test_panel_loads_detections_for_first_campaign(panel: ExaminePanel) -> None:
    # "All campaigns" is selected by default, so 3 rows * 2 campaigns = 6.
    assert panel._model.rowCount() == 6
    assert panel.ui.campaign_combo.count() == 3  # All + alpha + beta
    assert "6 / 6 detections" in panel.ui.info_label.text()


def test_max_per_filter_truncates_displayed_rows(panel: ExaminePanel) -> None:
    panel.ui.max_per_spin.setValue(1)
    # 1 per (ARU, Species) per campaign, so 1 * 2 campaigns = 2 rows
    assert panel._model.rowCount() == 2


def test_edit_verified_marks_row_dirty(panel: ExaminePanel) -> None:
    col = COLUMNS_BY_NAME["Verified"]
    idx = panel._model.index(0, col)
    assert panel._model.setData(idx, "true")
    dirty = panel._model.take_dirty()
    assert len(dirty) == 1
    assert dirty[0].verified.value == "true"


def test_autosave_debounces_and_persists(qtbot, panel: ExaminePanel, project) -> None:
    """Editing a Verified cell should auto-save to disk after the debounce."""
    col = COLUMNS_BY_NAME["Verified"]
    idx = panel._model.index(0, col)
    # Pick the row whose campaign we'll re-read after the save.
    detection = panel._model.detection_at(0)
    assert detection is not None
    csv_path = project.folder / detection.campaign / "detections-BirdNET-2.4.csv"

    panel._model.setData(idx, "true")
    # Auto-save runs after the debounce window. Wait for the timer to fire and
    # the CSV write to land.
    qtbot.waitUntil(lambda: not panel._autosave_timer.isActive(), timeout=2000)
    qtbot.waitUntil(lambda: "true" in csv_path.read_text(encoding="utf-8"), timeout=2000)

    # And take_dirty should now be empty: the autosave consumed the dirty set.
    assert panel._model.take_dirty() == []


def test_autosave_preserves_unedited_rows(qtbot, panel: ExaminePanel, project) -> None:
    """Auto-save must rewrite the campaign CSV with the FULL row set, not just
    the dirty rows. Regression test: an earlier version passed only take_dirty()
    to the repo, which overwrote the file with one row and dropped the others.
    """
    col = COLUMNS_BY_NAME["Verified"]
    idx = panel._model.index(0, col)
    detection = panel._model.detection_at(0)
    assert detection is not None
    csv_path = project.folder / detection.campaign / "detections-BirdNET-2.4.csv"

    rows_before = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(rows_before) == 4  # header + 3 fixture rows

    panel._model.setData(idx, "true")
    qtbot.waitUntil(lambda: not panel._autosave_timer.isActive(), timeout=2000)
    qtbot.waitUntil(lambda: "true" in csv_path.read_text(encoding="utf-8"), timeout=2000)

    rows_after = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(rows_after) == 4, "auto-save dropped unedited rows"


def test_combo_delegate_choices_for_verified(panel: ExaminePanel) -> None:
    """The Verified column delegate must offer the four canonical values."""
    from PySide6.QtWidgets import QComboBox, QStyleOptionViewItem

    from pam_analyzer.widgets.combo_delegate import ComboDelegate

    delegate = panel.ui.detections_table.table().itemDelegateForColumn(COLUMNS_BY_NAME["Verified"])
    assert isinstance(delegate, ComboDelegate)
    idx = panel._model.index(0, COLUMNS_BY_NAME["Verified"])
    editor = delegate.createEditor(panel.ui.detections_table.table(), QStyleOptionViewItem(), idx)
    assert isinstance(editor, QComboBox)
    assert [editor.itemText(i) for i in range(editor.count())] == [
        "",
        "true",
        "false",
        "uncertain",
    ]


def test_padding_spinboxes_init_from_project(qtbot, project: Project) -> None:
    """Loading a project populates the padding spinboxes from its TOML values."""
    # Bake non-zero padding into the project file.
    from dataclasses import replace

    from pam_analyzer.infrastructure import TomlProjectRepository

    p = replace(project, snippet_padding_before=1.5, snippet_padding_after=2.0)
    TomlProjectRepository().save(p)

    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    panel = ExaminePanel(state, CsvDetectionRepository(), AppSettings(), SoundfileAudioExtractor())
    qtbot.addWidget(panel)
    state.load_project(p.folder)

    assert panel.pad_before_spin.value() == pytest.approx(1.5)
    assert panel.pad_after_spin.value() == pytest.approx(2.0)


def test_changing_padding_persists_to_project_toml(panel: ExaminePanel, project: Project) -> None:
    """Editing a padding spinbox writes the new value back to pam-analyzer.toml."""
    panel.pad_before_spin.setValue(3.5)
    panel.pad_after_spin.setValue(0.5)

    from pam_analyzer.infrastructure import TomlProjectRepository

    reloaded = TomlProjectRepository().load(project.folder)
    assert reloaded.snippet_padding_before == pytest.approx(3.5)
    assert reloaded.snippet_padding_after == pytest.approx(0.5)


def test_project_toml_without_padding_loads_with_zero(qtbot, tmp_path: Path) -> None:
    """A pam-analyzer.toml written before snippet_padding_* existed must still load."""
    from pam_analyzer.infrastructure import paths

    paths.project_toml(tmp_path).write_text(
        '[project]\nsdcard_name_pattern = "^X-"\n',
        encoding="utf-8",
    )

    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    panel = ExaminePanel(state, CsvDetectionRepository(), AppSettings(), SoundfileAudioExtractor())
    qtbot.addWidget(panel)
    state.load_project(tmp_path)

    assert panel.pad_before_spin.value() == 0.0
    assert panel.pad_after_spin.value() == 0.0


def test_hidden_columns_persist_across_panel_instances(qtbot, project: Project) -> None:
    """Toggling a column off and rebuilding the panel restores the hidden state."""
    state = AppState(TomlProjectRepository(), TomlCampaignRepository())
    settings = AppSettings()
    panel = ExaminePanel(state, CsvDetectionRepository(), settings, SoundfileAudioExtractor())
    qtbot.addWidget(panel)
    state.load_project(project.folder)

    rank_col = COLUMNS_BY_NAME["Rank"]
    panel.ui.detections_table._toggle_column(rank_col, False)
    assert "Rank" in settings.examine_hidden_columns

    # Build a fresh panel against the same QSettings store.
    state2 = AppState(TomlProjectRepository(), TomlCampaignRepository())
    panel2 = ExaminePanel(state2, CsvDetectionRepository(), AppSettings(), SoundfileAudioExtractor())
    qtbot.addWidget(panel2)
    state2.load_project(project.folder)
    assert panel2.ui.detections_table._table.isColumnHidden(rank_col)


def _trigger_export_action(panel: ExaminePanel, label: str) -> None:
    """Trigger the QAction in the export menu whose text starts with *label*."""
    menu = panel.ui.export_button.menu()
    assert menu is not None, "export button has no menu attached"
    for action in menu.actions():
        if action.text().startswith(label):
            action.trigger()
            return
    raise AssertionError(f"export menu has no action labelled {label!r}")


def test_export_csv_writes_visible_rows(panel: ExaminePanel, tmp_path: Path, monkeypatch) -> None:
    """Export CSV must write the currently visible rows to the chosen path."""
    out = tmp_path / "exported.csv"
    monkeypatch.setattr(
        "pam_analyzer.ui.panels.examine_panel.QFileDialog.getSaveFileName",
        lambda *_a, **_k: (str(out), "CSV files (*.csv)"),
    )
    _trigger_export_action(panel, "Export CSV")

    assert out.exists()
    rows = out.read_text(encoding="utf-8").splitlines()
    # 6 rows in fixture (3 per campaign × 2 campaigns) + header
    assert len(rows) == 7


def test_export_csv_skips_hidden_columns(panel: ExaminePanel, tmp_path: Path, monkeypatch) -> None:
    """Hidden columns must not appear in the exported CSV header."""
    rank_col = COLUMNS_BY_NAME["Rank"]
    panel.ui.detections_table._toggle_column(rank_col, False)

    out = tmp_path / "exported.csv"
    monkeypatch.setattr(
        "pam_analyzer.ui.panels.examine_panel.QFileDialog.getSaveFileName",
        lambda *_a, **_k: (str(out), "CSV files (*.csv)"),
    )
    _trigger_export_action(panel, "Export CSV")

    header = out.read_text(encoding="utf-8").splitlines()[0]
    assert "Rank" not in header.split(",")
    assert "Campaign" in header.split(",")


def test_export_snippets_uses_padding(panel: ExaminePanel, project: Project, tmp_path: Path, monkeypatch) -> None:
    """Export snippets must call the extractor with start/end padded by the
    project's snippet_padding_before/_after."""
    # Set padding to a known non-zero value via the in-memory project.
    panel.pad_before_spin.setValue(0.5)
    panel.pad_after_spin.setValue(1.0)

    folder = tmp_path / "snips"
    folder.mkdir()
    monkeypatch.setattr(
        "pam_analyzer.ui.panels.examine_panel.QFileDialog.getExistingDirectory",
        lambda *_a, **_k: str(folder),
    )
    # Stub the extractor so the test doesn't need real WAVs on disk.
    calls: list[tuple[Path, float, float, Path]] = []

    def fake_extract(src, start, end, dst):
        calls.append((src, start, end, dst))

    monkeypatch.setattr(panel._audio_extractor, "extract", fake_extract)
    # The fixture rows reference 'f.wav' which doesn't exist, so synthesize it.
    audio_root = project.folder
    for d in panel._raw_detections:
        f = audio_root / d.file
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"")  # presence check only; extractor is stubbed

    _trigger_export_action(panel, "Export audio snippets")

    # Each visible detection should have produced one extract call.
    assert len(calls) == panel._model.rowCount()
    # Verify the start/end were padded.
    sample = calls[0]
    src, start, end, dst = sample
    detection = next(d for d in panel._model.detections() if d.file in src.as_posix())
    assert start == pytest.approx(max(0.0, detection.start_time - 0.5))
    assert end == pytest.approx(detection.end_time + 1.0)
    assert dst.parent == folder
    assert dst.suffix == ".wav"


def test_combo_delegate_species_choices_reflect_data(panel: ExaminePanel) -> None:
    """Corrected_Species choices must include every loaded species (deduped, sorted)."""
    from PySide6.QtWidgets import QComboBox, QStyleOptionViewItem

    from pam_analyzer.widgets.combo_delegate import ComboDelegate

    delegate = panel.ui.detections_table.table().itemDelegateForColumn(COLUMNS_BY_NAME["Corrected_Species"])
    assert isinstance(delegate, ComboDelegate)
    idx = panel._model.index(0, COLUMNS_BY_NAME["Corrected_Species"])
    editor = delegate.createEditor(panel.ui.detections_table.table(), QStyleOptionViewItem(), idx)
    assert isinstance(editor, QComboBox)
    items = [editor.itemText(i) for i in range(editor.count())]
    assert items[0] == ""
    # Fixture creates a single 'Robin' species across rows.
    assert "Robin" in items


def test_filter_inputs_visible_when_mounted_in_hidden_tab(qtbot, project: Project) -> None:
    """All filter inputs must be visible after switching to a tab that was hidden at setModel time."""
    state = AppState(TomlProjectRepository(), TomlCampaignRepository())

    # Mount ExaminePanel in a QTabWidget but keep a different tab active first.
    tabs = QTabWidget()
    dummy = QWidget()
    tabs.addTab(dummy, "Other")
    panel = ExaminePanel(state, CsvDetectionRepository(), AppSettings(), SoundfileAudioExtractor())
    tabs.addTab(panel, "Examine")
    qtbot.addWidget(tabs)
    tabs.show()
    tabs.setCurrentIndex(0)  # Examine tab is hidden

    # Load project while ExaminePanel is not visible. This triggers setModel.
    state.load_project(project.folder)
    qtbot.waitExposed(tabs)

    # Switch to Examine tab and let Qt settle geometry.
    tabs.setCurrentIndex(1)
    qtbot.waitExposed(panel)
    QCoreApplication.processEvents()

    detection_table = panel.ui.detections_table
    filter_row = detection_table._filter_row
    col_count = panel._model.columnCount()
    # Find the rightmost non-suppressed, non-hidden column and assert its filter is visible.
    rightmost = None
    for col in range(col_count - 1, -1, -1):
        if not detection_table._table.isColumnHidden(col) and filter_row.is_filter_visible(col):
            rightmost = col
            break
    assert rightmost is not None, "No visible, non-suppressed column found"
    assert filter_row.is_filter_visible(rightmost), (
        f"Filter input for column {rightmost} is not visible after switching to a previously-hidden tab"
    )


def test_text_filter_contains(panel: ExaminePanel) -> None:
    panel._model.set_column_filter(COLUMNS_BY_NAME["ARU"], "MSD-1", FilterOp.CONTAINS)
    rows = panel._model.detections()
    assert rows
    assert all(r.aru.startswith("MSD-1") for r in rows)


def test_text_filter_equals_excludes_substrings(panel: ExaminePanel) -> None:
    panel._model.set_column_filter(COLUMNS_BY_NAME["ARU"], "MSD-1", FilterOp.EQUALS)
    # Only an exact "MSD-1" match should remain. The fixture uses MSD-1 and MSD-2.
    rows = panel._model.detections()
    assert rows and all(r.aru == "MSD-1" for r in rows)


def test_numeric_filter_greater_than(panel: ExaminePanel) -> None:
    # Fixture confidences are 0.5, 0.6, 0.7 per campaign. > 0.55 keeps 4 rows.
    panel._model.set_column_filter(COLUMNS_BY_NAME["Confidence"], "0.55", FilterOp.GREATER_THAN)
    rows = panel._model.detections()
    assert len(rows) == 4
    assert all(r.confidence > 0.55 for r in rows)


def test_numeric_filter_in_range(panel: ExaminePanel) -> None:
    panel._model.set_column_filter(COLUMNS_BY_NAME["Confidence"], "0.55 - 0.65", FilterOp.IN_RANGE)
    rows = panel._model.detections()
    assert len(rows) == 2
    assert all(0.55 <= r.confidence <= 0.65 for r in rows)


def test_blank_filter_on_comment_field(panel: ExaminePanel) -> None:
    # Fixture leaves Comment empty for every row.
    panel._model.set_column_filter(COLUMNS_BY_NAME["Comment"], "", FilterOp.BLANK)
    assert panel._model.rowCount() == 6
    panel._model.set_column_filter(COLUMNS_BY_NAME["Comment"], "", FilterOp.NOT_BLANK)
    assert panel._model.rowCount() == 0


def test_clear_filter_via_empty_text_with_default_op(panel: ExaminePanel) -> None:
    panel._model.set_column_filter(COLUMNS_BY_NAME["ARU"], "MSD-1", FilterOp.CONTAINS)
    assert panel._model.rowCount() == 3
    panel._model.set_column_filter(COLUMNS_BY_NAME["ARU"], "", FilterOp.CONTAINS)
    assert panel._model.rowCount() == 6


def test_date_range_filter(panel: ExaminePanel) -> None:
    # Fixture dates are 2026-04-25/26/27 (one per row, both campaigns).
    col = panel._model.index_of("Recording_Time")
    panel._model.set_column_filter(col, "2026-04-25 .. 2026-04-26", FilterOp.DATE_RANGE)
    rows = panel._model.detections()
    assert len(rows) == 4
    assert all(r.recording_time[:10] in ("2026-04-25", "2026-04-26") for r in rows)


def test_on_date_filter(panel: ExaminePanel) -> None:
    col = panel._model.index_of("Recording_Time")
    panel._model.set_column_filter(col, "2026-04-26", FilterOp.ON_DATE)
    rows = panel._model.detections()
    assert len(rows) == 2
    assert all(r.recording_time.startswith("2026-04-26") for r in rows)


def test_time_of_day_filter(panel: ExaminePanel) -> None:
    # Fixture times of day are 04:00/05:00/06:00 (one per row, both campaigns).
    col = panel._model.index_of("Recording_Time")
    panel._model.set_column_filter(col, "04:30 - 06:30", FilterOp.TIME_OF_DAY_RANGE)
    assert panel._model.rowCount() == 4


def test_time_of_day_filter_wraps_midnight(panel: ExaminePanel) -> None:
    col = panel._model.index_of("Recording_Time")
    panel._model.set_column_filter(col, "22:00 - 04:30", FilterOp.TIME_OF_DAY_RANGE)
    rows = panel._model.detections()
    assert len(rows) == 2
    assert all("T04:00" in r.recording_time for r in rows)


def test_is_any_of_filter(panel: ExaminePanel) -> None:
    col = panel._model.index_of("ARU")
    panel._model.set_column_filter(col, "MSD-1; MSD-2", FilterOp.IS_ANY_OF)
    assert panel._model.rowCount() == 6
    panel._model.set_column_filter(col, "MSD-1", FilterOp.IS_ANY_OF)
    rows = panel._model.detections()
    assert rows and all(r.aru == "MSD-1" for r in rows)


def test_distinct_values_for_set_popup(panel: ExaminePanel) -> None:
    assert panel._model.distinct_values(panel._model.index_of("ARU")) == ["MSD-1", "MSD-2"]
    # The play column never offers values.
    assert panel._model.distinct_values(0) == []


def test_funnel_menu_is_one_of_flow(panel: ExaminePanel) -> None:
    """End-to-end: pick "Is one of..." in the funnel menu, check a value in
    the set popup, apply, and see the canonical text and filtered rows.

    QMenu.exec blocks, so both popups are driven from single-shot timers
    (monkeypatching QMenu.exec on the class does not intercept in PySide6).
    The set popup opens via its own deferred single-shot after the op menu
    closes, so the second handler retries until it appears.
    """
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMenu, QPushButton

    from pam_analyzer.widgets.filter_popups import SetPopup

    detection_table = panel.ui.detections_table
    filter_row = detection_table._filter_row
    col = panel._model.index_of("ARU")

    def _handle_set_popup(attempts: int = 0) -> None:
        popup_menu = QApplication.activePopupWidget()
        set_popup = popup_menu.findChild(SetPopup) if popup_menu else None
        if set_popup is None:
            assert attempts < 50, "set popup never appeared"
            QTimer.singleShot(20, lambda: _handle_set_popup(attempts + 1))
            return
        set_popup._list.item(0).setCheckState(Qt.CheckState.Checked)
        apply_button = next(
            b for b in set_popup.findChildren(QPushButton) if b.text() == "Apply"
        )
        apply_button.click()

    def _pick_is_one_of() -> None:
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        action = next(a for a in menu.actions() if a.text() == "Is one of...")
        QTimer.singleShot(20, _handle_set_popup)
        action.trigger()
        menu.close()

    QTimer.singleShot(0, _pick_is_one_of)
    filter_row._show_op_menu(col)
    QCoreApplication.processEvents()

    assert filter_row._slots[col].edit.text() == "MSD-1"
    assert filter_row.column_op(col) is FilterOp.IS_ANY_OF
    rows = panel._model.detections()
    assert rows and all(r.aru == "MSD-1" for r in rows)


def test_typing_a_filter_keeps_focus_in_the_filter_input(qtbot, panel: ExaminePanel) -> None:
    """Filtering out the selected row must not move focus to the table,
    where the armed shortcuts would swallow the rest of the typing."""
    from PySide6.QtWidgets import QApplication

    panel.show()
    qtbot.waitExposed(panel)
    filter_row = panel.ui.detections_table._filter_row
    edit = filter_row._slots[panel._model.index_of("ARU")].edit
    edit.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is edit)

    qtbot.keyClicks(edit, "MSD-2")
    # Wait out the debounce until the filter has applied (row selection and
    # player sync included).
    qtbot.waitUntil(lambda: panel._model.rowCount() == 3)
    QCoreApplication.processEvents()

    assert QApplication.focusWidget() is edit


def test_enter_in_filter_input_applies_and_focuses_table(qtbot, panel: ExaminePanel) -> None:
    from PySide6.QtWidgets import QApplication

    panel.show()
    qtbot.waitExposed(panel)
    detection_table = panel.ui.detections_table
    edit = detection_table._filter_row._slots[panel._model.index_of("ARU")].edit
    edit.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is edit)

    qtbot.keyClicks(edit, "MSD-2")
    qtbot.keyClick(edit, Qt.Key.Key_Return)

    # Applied immediately, without waiting out the 300 ms debounce.
    assert panel._model.rowCount() == 3
    focused = QApplication.focusWidget()
    assert focused in (detection_table._table, detection_table._table.viewport())
    # Drain the deferred player prepare (armed by the row auto-select) inside
    # the test; firing during teardown would touch half-destroyed widgets.
    QCoreApplication.processEvents()


def test_column_menu_no_qaction_error(panel: ExaminePanel) -> None:
    """Column header context menu must not raise NameError (B1 regression: QAction removed from imports)."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    detection_table = panel.ui.detections_table
    # Close the popup in the next event-loop tick so exec() unblocks.
    QTimer.singleShot(0, lambda: (w := QApplication.activePopupWidget()) and w.close())
    detection_table._show_column_menu(QPoint(100, 10))
    assert detection_table._model.columnCount() > 1
