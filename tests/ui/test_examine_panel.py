"""pytest-qt smoke tests for the Examine panel."""

import csv
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QPoint, Qt
from PySide6.QtWidgets import QProgressDialog, QTabWidget, QWidget

from pam_analyzer.domain import Campaign, Detection, FilterMode, LatLon, Project
from pam_analyzer.domain.filter_ops import FilterOp
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.models.detections_table_model import COLUMNS_BY_NAME
from pam_analyzer.ui.panels.examine_panel import ExaminePanel
from pam_analyzer.ui.settings import AppSettings
from tests.conftest import DEFAULT_MODEL_KEY


def _visible(panel: ExaminePanel) -> list[Detection]:
    model = panel._model
    return [model.detection_at(r) for r in range(model.rowCount())]

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

    for name, aru in (("alpha", "MSD-1"), ("beta", "MSD-2")):
        folder = project_folder / name
        folder.mkdir()
        Campaign(
            name=name,
            folder=folder,
            species_filter_mode=FilterMode.LOCATION,
            location=LatLon(48.0, 11.0),
        ).save()
        csv_path = folder / f"detections-{DEFAULT_MODEL_KEY}.csv"
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
    proj.save()
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
def panel(qtbot, project: Project, load_project) -> ExaminePanel:
    state = AppState()
    panel = ExaminePanel(state, AppSettings())
    qtbot.addWidget(panel)
    load_project(state, project.folder)
    # ExaminePanel coalesces its campaign reload onto a single-shot timer, so pump
    # the event loop once to let the detections load before the test inspects them.
    QCoreApplication.processEvents()
    return panel


def test_panel_loads_detections_for_first_campaign(panel: ExaminePanel) -> None:
    # "All campaigns" is selected by default, so 3 rows * 2 campaigns = 6.
    assert panel._model.rowCount() == 6
    assert panel.ui.campaign_combo.count() == 3  # All + alpha + beta
    # Unfiltered, so the count collapses to a single number (no "of").
    assert "6 detections" in panel.ui.info_label.text()
    assert " of " not in panel.ui.info_label.text()
    # A single model run carries no breakdown suffix (it would just repeat the count).
    assert "[" not in panel.ui.info_label.text()


def test_info_label_breaks_down_counts_by_model(
    panel: ExaminePanel, project: Project, monkeypatch
) -> None:
    """A campaign with two model runs shows a per-model total after the count."""
    # A campaign switch selects a row, which schedules a deferred audio prepare
    # that would try to open the dummy f.wav. Patch the presentation call it
    # ends up in (the timer is already bound to the real _do_prepare, so
    # patching that method wouldn't take); this test is about label text, not
    # playback.
    monkeypatch.setattr(panel.ui.detections_table, "_present", lambda *a, **k: None)

    # Alpha ends up with a current run plus a CSV from the retired Perch
    # model, which is what a campaign analyzed before the upgrade looks like.
    # The base fixture omits the Model column, so its rows would load as model
    # "" and show up as a third "unknown" group; overwrite that same file with
    # one that names its model, matching what a real runner writes.
    _write_model_csv(
        project.folder / "alpha" / f"detections-{DEFAULT_MODEL_KEY}.csv", "alpha", "MSD-1", DEFAULT_MODEL_KEY, rows=3
    )
    _write_model_csv(project.folder / "alpha" / "detections-Perch-2.0.csv", "alpha", "MSD-1", "Perch-2.0", rows=2)

    # Switch to the alpha campaign (found by data, since combo order isn't
    # guaranteed). This is a synchronous reload, and it excludes beta's
    # model-less rows so the breakdown is a clean two-model split.
    panel.ui.campaign_combo.setCurrentIndex(panel.ui.campaign_combo.findData("alpha"))

    text = panel.ui.info_label.text()
    # alpha only: 3 current + 2 Perch = 5. Unfiltered, so every cell collapses
    # to a single number.
    assert "5 detections" in text
    assert f"[{DEFAULT_MODEL_KEY}: 3]   [Perch-2.0: 2]" in text

    # A cap narrows the view to the single highest-confidence row per
    # (ARU, Species). That row is a current-model one (conf 0.7 > any Perch
    # row), so its cell shows the shown-of-total split while Perch drops to
    # 0 of 2. Perch stays listed (order and membership come from the totals).
    panel.ui.max_per_spin.setValue(1)
    text = panel.ui.info_label.text()
    assert "1 of 5 detections" in text
    assert f"[{DEFAULT_MODEL_KEY}: 1 of 3]   [Perch-2.0: 0 of 2]" in text


def _write_model_csv(path: Path, campaign: str, aru: str, model: str, rows: int) -> None:
    """Write a detections CSV that includes a populated Model column."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([*_HEADERS, "Model"])
        for i in range(rows):
            w.writerow(
                [
                    campaign,
                    aru,
                    "24",
                    "Robin",
                    "Erithacus rubecula",
                    f"{0.5 + i * 0.1}",
                    f"{i * 3.0}",
                    f"{i * 3.0 + 3.0}",
                    str(i + 1),
                    "f.wav",
                    f"2026-04-{25 + i:02d}T{4 + i:02d}:00:00",
                    "",
                    "",
                    "",
                    model,
                ]
            )


def test_max_per_filter_truncates_displayed_rows(panel: ExaminePanel) -> None:
    panel.ui.max_per_spin.setValue(1)
    # 1 per (ARU, Species) per campaign, so 1 * 2 campaigns = 2 rows
    assert panel._model.rowCount() == 2


def test_column_filter_survives_max_per_change(panel: ExaminePanel) -> None:
    """Regression: adjusting Max per ARU/Species must not drop an active column filter."""
    aru_col = COLUMNS_BY_NAME["ARU"]
    panel._model.set_column_filter(aru_col, "MSD-1", FilterOp.EQUALS)
    assert panel._model.rowCount() == 3  # only the alpha/MSD-1 rows

    panel.ui.max_per_spin.setValue(1)
    # Filter first (3 MSD-1 Robin rows), then cap to the single best by confidence.
    assert panel._model.rowCount() == 1
    conf_col = panel._model.index_of("Confidence")
    assert float(panel._model.data(panel._model.index(0, conf_col))) == pytest.approx(0.7)

    # Turning the cap back off must reveal all three MSD-1 rows again, proving
    # the ARU filter stayed active throughout rather than being cleared.
    panel.ui.max_per_spin.setValue(0)
    assert panel._model.rowCount() == 3


def test_max_per_caps_after_column_filter(panel: ExaminePanel) -> None:
    """The cap ranks only rows that survive the column filters (filter first, then cap)."""
    aru_col = COLUMNS_BY_NAME["ARU"]
    conf_col = COLUMNS_BY_NAME["Confidence"]
    panel._model.set_column_filter(aru_col, "MSD-1", FilterOp.EQUALS)
    # Exclude the overall-best 0.7 row, leaving 0.5 and 0.6.
    panel._model.set_column_filter(conf_col, "0.7", FilterOp.LESS_THAN)
    panel.ui.max_per_spin.setValue(1)
    # Cap-first would rank 0.7 top then drop it (0 rows); filter-first keeps 0.6.
    assert panel._model.rowCount() == 1
    assert float(panel._model.data(panel._model.index(0, conf_col))) == pytest.approx(0.6)


def test_edit_verified_marks_row_dirty(panel: ExaminePanel) -> None:
    col = COLUMNS_BY_NAME["Verified"]
    idx = panel._model.index(0, col)
    assert panel._model.setData(idx, "true")
    assert panel._detections.dirty_count == 1
    assert panel._model.detection_at(0).verified.value == "true"


def test_autosave_debounces_and_persists(qtbot, panel: ExaminePanel, project) -> None:
    """Editing a Verified cell should auto-save to disk after the debounce."""
    col = COLUMNS_BY_NAME["Verified"]
    idx = panel._model.index(0, col)
    # Pick the row whose campaign we'll re-read after the save.
    detection = panel._model.detection_at(0)
    assert detection is not None
    csv_path = project.folder / detection.campaign / f"detections-{DEFAULT_MODEL_KEY}.csv"

    panel._model.setData(idx, "true")
    # Auto-save runs after the debounce window. Wait for the timer to fire and
    # the CSV write to land.
    qtbot.waitUntil(lambda: not panel._autosave_timer.isActive(), timeout=2000)
    qtbot.waitUntil(lambda: "true" in csv_path.read_text(encoding="utf-8"), timeout=2000)

    # The autosave consumed the pending edits.
    assert panel._detections.dirty_count == 0


def test_autosave_preserves_unedited_rows(qtbot, panel: ExaminePanel, project) -> None:
    """Auto-save must rewrite the campaign CSV with the FULL row set, not just
    the dirty rows. Regression test: an earlier version passed only the dirty rows
    to the repo, which overwrote the file with one row and dropped the others.
    """
    col = COLUMNS_BY_NAME["Verified"]
    idx = panel._model.index(0, col)
    detection = panel._model.detection_at(0)
    assert detection is not None
    csv_path = project.folder / detection.campaign / f"detections-{DEFAULT_MODEL_KEY}.csv"

    rows_before = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(rows_before) == 4  # header + 3 fixture rows

    panel._model.setData(idx, "true")
    qtbot.waitUntil(lambda: not panel._autosave_timer.isActive(), timeout=2000)
    qtbot.waitUntil(lambda: "true" in csv_path.read_text(encoding="utf-8"), timeout=2000)

    rows_after = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(rows_after) == 4, "auto-save dropped unedited rows"


def test_autosave_rewrites_only_the_edited_campaign(qtbot, panel: ExaminePanel, project) -> None:
    """With All campaigns loaded, an edit must leave the other campaigns' CSVs alone.

    Rewriting every CSV per edit froze the UI for seconds on a 600k-row project.
    """
    detection = panel._model.detection_at(0)
    assert detection is not None
    other = next(c for c in ("alpha", "beta") if c != detection.campaign)
    other_csv = project.folder / other / f"detections-{DEFAULT_MODEL_KEY}.csv"
    # A marker only a rewrite would drop, since the rewritten content is otherwise identical.
    other_csv.write_text(other_csv.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    marked = other_csv.read_bytes()

    panel._model.setData(panel._model.index(0, COLUMNS_BY_NAME["Verified"]), "true")
    edited_csv = project.folder / detection.campaign / f"detections-{DEFAULT_MODEL_KEY}.csv"
    qtbot.waitUntil(lambda: "true" in edited_csv.read_text(encoding="utf-8"), timeout=2000)

    assert other_csv.read_bytes() == marked


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


def test_single_click_opens_the_dropdown_of_an_unselected_row(qtbot, panel: ExaminePanel, monkeypatch) -> None:
    from PySide6.QtWidgets import QApplication, QComboBox

    monkeypatch.setattr(panel.ui.detections_table, "_present", lambda *a, **k: None)
    panel.show()
    qtbot.waitExposed(panel)
    view = panel.ui.detections_table.table()

    verified = view.model().index(2, panel._model.index_of("Verified"))
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(verified).center())
    qtbot.waitUntil(lambda: isinstance(QApplication.activePopupWidget(), QWidget))
    assert isinstance(view.indexWidget(verified), QComboBox)
    QApplication.activePopupWidget().close()
    QCoreApplication.processEvents()


def test_single_click_on_comment_accepts_typing_until_return(qtbot, panel: ExaminePanel, monkeypatch) -> None:
    from PySide6.QtWidgets import QAbstractItemView, QLineEdit

    monkeypatch.setattr(panel.ui.detections_table, "_present", lambda *a, **k: None)
    panel.show()
    qtbot.waitExposed(panel)
    view = panel.ui.detections_table.table()

    comment = view.model().index(2, panel._model.index_of("Comment"))
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(comment).center())
    editor = view.indexWidget(comment)
    assert isinstance(editor, QLineEdit)
    qtbot.keyClicks(editor, "two calls")
    qtbot.keyClick(editor, Qt.Key.Key_Return)

    qtbot.waitUntil(lambda: view.state() != QAbstractItemView.State.EditingState)
    assert comment.data() == "two calls"
    QCoreApplication.processEvents()


def test_padding_spinboxes_init_from_project(qtbot, project: Project, load_project) -> None:
    """Loading a project populates the padding spinboxes from its TOML values."""
    # Bake non-zero padding into the project file.
    from dataclasses import replace


    p = replace(project, snippet_padding_before=1.5, snippet_padding_after=2.0)
    p.save()

    state = AppState()
    panel = ExaminePanel(state, AppSettings())
    qtbot.addWidget(panel)
    load_project(state, p.folder)

    assert panel.pad_before_spin.value() == pytest.approx(1.5)
    assert panel.pad_after_spin.value() == pytest.approx(2.0)


def test_changing_padding_persists_to_project_toml(panel: ExaminePanel, project: Project) -> None:
    """Editing a padding spinbox writes the new value back to pam-analyzer.toml."""
    panel.pad_before_spin.setValue(3.5)
    panel.pad_after_spin.setValue(0.5)


    reloaded = Project.load(project.folder)
    assert reloaded.snippet_padding_before == pytest.approx(3.5)
    assert reloaded.snippet_padding_after == pytest.approx(0.5)


def test_project_toml_without_padding_loads_with_zero(
    qtbot, tmp_path: Path, load_project
) -> None:
    """A pam-analyzer.toml written before snippet_padding_* existed must still load."""
    from pam_analyzer.domain import paths

    paths.project_toml(tmp_path).write_text(
        '[project]\nsdcard_name_pattern = "^X-"\n',
        encoding="utf-8",
    )

    state = AppState()
    panel = ExaminePanel(state, AppSettings())
    qtbot.addWidget(panel)
    load_project(state, tmp_path)

    assert panel.pad_before_spin.value() == 0.0
    assert panel.pad_after_spin.value() == 0.0


def test_hidden_columns_persist_across_panel_instances(
    qtbot, project: Project, load_project
) -> None:
    """Toggling a column off and rebuilding the panel restores the hidden state."""
    state = AppState()
    settings = AppSettings()
    panel = ExaminePanel(state, settings)
    qtbot.addWidget(panel)
    load_project(state, project.folder)

    rank_col = COLUMNS_BY_NAME["Rank"]
    panel.ui.detections_table._toggle_column(rank_col, False)
    assert "Rank" in settings.examine_hidden_columns

    # Build a fresh panel against the same QSettings store.
    state2 = AppState()
    panel2 = ExaminePanel(state2, AppSettings())
    qtbot.addWidget(panel2)
    load_project(state2, project.folder)
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


def test_export_csv_toasts_success_with_folder_link(
    panel: ExaminePanel, tmp_path: Path, monkeypatch, toasts
) -> None:
    out = tmp_path / "exported.csv"
    monkeypatch.setattr(
        "pam_analyzer.ui.panels.examine_panel.QFileDialog.getSaveFileName",
        lambda *_a, **_k: (str(out), "CSV files (*.csv)"),
    )
    opened: list[Path] = []
    monkeypatch.setattr("pam_analyzer.ui.panels.examine_panel.open_in_file_manager", opened.append)
    _trigger_export_action(panel, "Export CSV")

    [(kind, _title, text, kwargs)] = toasts
    assert kind == "success"
    assert "6 rows" in text
    dict(kwargs["links"])["Show in file manager"]()
    assert opened == [tmp_path]


def test_export_actions_disabled_when_no_rows_are_shown(panel: ExaminePanel) -> None:
    actions = panel.ui.export_button.menu().actions()
    assert all(a.isEnabled() for a in actions)

    panel._model.set_column_filter(COLUMNS_BY_NAME["ARU"], "no-such-aru", FilterOp.EQUALS)
    assert not any(a.isEnabled() for a in actions)

    panel._model.set_column_filter(COLUMNS_BY_NAME["ARU"], "", FilterOp.EQUALS)
    assert all(a.isEnabled() for a in actions)


def test_export_csv_toasts_error_on_write_failure(
    panel: ExaminePanel, tmp_path: Path, monkeypatch, toasts
) -> None:
    out = tmp_path / "missing_dir" / "exported.csv"
    monkeypatch.setattr(
        "pam_analyzer.ui.panels.examine_panel.QFileDialog.getSaveFileName",
        lambda *_a, **_k: (str(out), "CSV files (*.csv)"),
    )
    _trigger_export_action(panel, "Export CSV")

    assert [t[0] for t in toasts] == ["error"]


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

    folder = _prepare_snippet_export(panel, project, tmp_path, monkeypatch)
    # Stub the extractor so the test doesn't need real WAVs on disk.
    calls: list[tuple[Path, float, float, Path]] = []

    def fake_extract(src, start, end, dst):
        calls.append((src, start, end, dst))

    monkeypatch.setattr("pam_analyzer.ui.panels.examine_panel.extract_snippet", fake_extract)
    _trigger_export_action(panel, "Export audio snippets")

    # Each visible detection should have produced one extract call.
    assert len(calls) == panel._model.rowCount()
    # Verify the start/end were padded.
    sample = calls[0]
    src, start, end, dst = sample
    detection = next(d for d in _visible(panel) if d.file in src.as_posix())
    assert start == pytest.approx(max(0.0, detection.start_time - 0.5))
    assert end == pytest.approx(detection.end_time + 1.0)
    assert dst.parent == folder
    assert dst.suffix == ".flac"


def _prepare_snippet_export(panel: ExaminePanel, project: Project, tmp_path: Path, monkeypatch) -> Path:
    """Point the folder dialog at a fresh folder and create the audio files the rows reference."""
    folder = tmp_path / "snips"
    folder.mkdir()
    monkeypatch.setattr(
        "pam_analyzer.ui.panels.examine_panel.QFileDialog.getExistingDirectory",
        lambda *_a, **_k: str(folder),
    )
    for rel in panel._detections.frame["File"].unique().to_list():
        f = project.folder / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"")  # presence check only, the extractor is stubbed
    return folder


def test_export_snippets_toasts_success(
    panel: ExaminePanel, project: Project, tmp_path: Path, monkeypatch, toasts
) -> None:
    folder = _prepare_snippet_export(panel, project, tmp_path, monkeypatch)
    monkeypatch.setattr("pam_analyzer.ui.panels.examine_panel.extract_snippet", lambda *_a: None)
    opened: list[Path] = []
    monkeypatch.setattr("pam_analyzer.ui.panels.examine_panel.open_in_file_manager", opened.append)
    _trigger_export_action(panel, "Export audio snippets")

    [(kind, _title, text, kwargs)] = toasts
    assert kind == "success"
    assert "6 snippets" in text
    dict(kwargs["links"])["Show in file manager"]()
    assert opened == [folder]


def test_export_snippets_toasts_warning_on_partial_failure(
    panel: ExaminePanel, project: Project, tmp_path: Path, monkeypatch, toasts
) -> None:
    _prepare_snippet_export(panel, project, tmp_path, monkeypatch)
    calls = 0

    def flaky_extract(*_a):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")

    monkeypatch.setattr("pam_analyzer.ui.panels.examine_panel.extract_snippet", flaky_extract)
    _trigger_export_action(panel, "Export audio snippets")

    [(kind, _title, text, kwargs)] = toasts
    assert kind == "warning"
    assert "Exported 5 of 6 snippets" in text
    assert "disk full" in text
    # Sticky, so the error stays readable until dismissed.
    assert kwargs["duration"] == 0


def test_export_snippets_cancel_stops_the_loop(
    panel: ExaminePanel, project: Project, tmp_path: Path, monkeypatch, toasts
) -> None:
    _prepare_snippet_export(panel, project, tmp_path, monkeypatch)
    calls = 0

    def cancelling_extract(*_a):
        nonlocal calls
        calls += 1
        if calls == 2:
            panel.window().findChild(QProgressDialog).cancel()

    monkeypatch.setattr("pam_analyzer.ui.panels.examine_panel.extract_snippet", cancelling_extract)
    _trigger_export_action(panel, "Export audio snippets")

    assert calls == 2
    [(kind, title, text, _kwargs)] = toasts
    assert kind == "warning"
    assert "cancelled" in title
    assert "Exported 2 of 6 snippets" in text


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


def test_filter_inputs_visible_when_mounted_in_hidden_tab(
    qtbot, project: Project, load_project
) -> None:
    """All filter inputs must be visible after switching to a tab that was hidden at setModel time."""
    state = AppState()

    # Mount ExaminePanel in a QTabWidget but keep a different tab active first.
    tabs = QTabWidget()
    dummy = QWidget()
    tabs.addTab(dummy, "Other")
    panel = ExaminePanel(state, AppSettings())
    tabs.addTab(panel, "Examine")
    qtbot.addWidget(tabs)
    tabs.show()
    tabs.setCurrentIndex(0)  # Examine tab is hidden

    # Load project while ExaminePanel is not visible. This triggers setModel.
    load_project(state, project.folder)
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
    rows = _visible(panel)
    assert rows
    assert all(r.aru.startswith("MSD-1") for r in rows)


def test_text_filter_equals_excludes_substrings(panel: ExaminePanel) -> None:
    panel._model.set_column_filter(COLUMNS_BY_NAME["ARU"], "MSD-1", FilterOp.EQUALS)
    # Only an exact "MSD-1" match should remain. The fixture uses MSD-1 and MSD-2.
    rows = _visible(panel)
    assert rows and all(r.aru == "MSD-1" for r in rows)


def test_numeric_filter_greater_than(panel: ExaminePanel) -> None:
    # Fixture confidences are 0.5, 0.6, 0.7 per campaign. > 0.55 keeps 4 rows.
    panel._model.set_column_filter(COLUMNS_BY_NAME["Confidence"], "0.55", FilterOp.GREATER_THAN)
    rows = _visible(panel)
    assert len(rows) == 4
    assert all(r.confidence > 0.55 for r in rows)


def test_numeric_filter_in_range(panel: ExaminePanel) -> None:
    panel._model.set_column_filter(COLUMNS_BY_NAME["Confidence"], "0.55 - 0.65", FilterOp.IN_RANGE)
    rows = _visible(panel)
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


def test_not_blank_filter_can_be_switched_back_from_the_funnel(qtbot, panel: ExaminePanel) -> None:
    """The funnel must stay clickable while a value-less op grays out the input."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from pam_analyzer.domain.filter_ops import default_op, label_for

    panel.show()
    qtbot.waitExposed(panel)
    filter_row = panel.ui.detections_table._filter_row
    col = panel._model.index_of("Comment")
    slot = filter_row._slots[col]
    original_op = default_op(slot.kind)

    filter_row._set_op(col, FilterOp.NOT_BLANK)
    assert panel._model.rowCount() == 0
    assert not slot.edit.isEnabled()

    def _pick_original_op() -> None:
        menu = QApplication.activePopupWidget()
        if menu is None:
            # A dead funnel opens no menu. The asserts below report it.
            return
        action = next(a for a in menu.actions() if a.text() == label_for(original_op))
        action.trigger()
        menu.close()

    QTimer.singleShot(0, _pick_original_op)
    qtbot.mouseClick(slot.button, Qt.MouseButton.LeftButton)
    QCoreApplication.processEvents()

    assert filter_row.column_op(col) is original_op
    assert slot.edit.isEnabled()
    assert panel._model.rowCount() == 6


def test_hiding_a_column_resets_its_blank_filter(panel: ExaminePanel) -> None:
    detection_table = panel.ui.detections_table
    filter_row = detection_table._filter_row
    col = panel._model.index_of("Comment")
    slot = filter_row._slots[col]
    original_op = filter_row.column_op(col)

    filter_row._set_op(col, FilterOp.NOT_BLANK)
    assert panel._model.rowCount() == 0

    detection_table._toggle_column(col, False)
    detection_table._toggle_column(col, True)

    assert panel._model.rowCount() == 6
    assert filter_row.column_op(col) is original_op
    assert slot.edit.isEnabled()
    QCoreApplication.processEvents()


def test_picking_the_checked_not_blank_again_lifts_the_filter(panel: ExaminePanel) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMenu

    from pam_analyzer.domain.filter_ops import label_for

    filter_row = panel.ui.detections_table._filter_row
    col = panel._model.index_of("Comment")
    slot = filter_row._slots[col]
    original_op = filter_row.column_op(col)

    def _pick_not_blank() -> None:
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        action = next(a for a in menu.actions() if a.text() == label_for(FilterOp.NOT_BLANK))
        action.trigger()
        menu.close()

    QTimer.singleShot(0, _pick_not_blank)
    filter_row._show_op_menu(col)
    assert filter_row.column_op(col) is FilterOp.NOT_BLANK
    assert panel._model.rowCount() == 0

    QTimer.singleShot(0, _pick_not_blank)
    filter_row._show_op_menu(col)
    assert filter_row.column_op(col) is original_op
    assert slot.edit.isEnabled()
    assert not slot.button._active
    assert panel._model.rowCount() == 6
    QCoreApplication.processEvents()


def test_date_range_filter(panel: ExaminePanel) -> None:
    # Fixture dates are 2026-04-25/26/27 (one per row, both campaigns).
    col = panel._model.index_of("Recording_Time")
    panel._model.set_column_filter(col, "2026-04-25 .. 2026-04-26", FilterOp.DATE_RANGE)
    rows = _visible(panel)
    assert len(rows) == 4
    assert all(r.recording_time[:10] in ("2026-04-25", "2026-04-26") for r in rows)


def test_on_date_filter(panel: ExaminePanel) -> None:
    col = panel._model.index_of("Recording_Time")
    panel._model.set_column_filter(col, "2026-04-26", FilterOp.ON_DATE)
    rows = _visible(panel)
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
    rows = _visible(panel)
    assert len(rows) == 2
    assert all("T04:00" in r.recording_time for r in rows)


def test_is_any_of_filter(panel: ExaminePanel) -> None:
    col = panel._model.index_of("ARU")
    panel._model.set_column_filter(col, "MSD-1; MSD-2", FilterOp.IS_ANY_OF)
    assert panel._model.rowCount() == 6
    panel._model.set_column_filter(col, "MSD-1", FilterOp.IS_ANY_OF)
    rows = _visible(panel)
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
    rows = _visible(panel)
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


def test_showing_the_panel_focuses_the_table(qtbot, panel: ExaminePanel) -> None:
    """Navigating to the panel must hand focus to the table.

    The single-key shortcuts (Space, T/F/U, ...) are WidgetShortcut-scoped to
    the inner table, so a pre-selected row alone leaves Space going nowhere.
    """
    from PySide6.QtWidgets import QApplication

    table = panel.ui.detections_table.table()
    panel.show()
    qtbot.waitExposed(panel)

    qtbot.waitUntil(lambda: QApplication.focusWidget() in (table, table.viewport()))
    # Drain the deferred player prepare armed by the row auto-select.
    QCoreApplication.processEvents()


def test_space_toggles_the_player_after_navigating_to_the_panel(qtbot, panel: ExaminePanel, monkeypatch) -> None:
    """End to end: the pre-selected row is playable with Space, no click first."""
    from PySide6.QtWidgets import QApplication

    detection_table = panel.ui.detections_table
    monkeypatch.setattr(detection_table, "_present", lambda *a, **k: None)
    toggled: list[bool] = []
    monkeypatch.setattr(detection_table._player, "isVisible", lambda: True)
    monkeypatch.setattr(detection_table._player, "toggle", lambda: toggled.append(True))

    panel.show()
    qtbot.waitExposed(panel)
    table = detection_table.table()
    qtbot.waitUntil(lambda: QApplication.focusWidget() in (table, table.viewport()))

    qtbot.keyClick(table, Qt.Key.Key_Space)
    assert toggled == [True]
    QCoreApplication.processEvents()


def test_reload_does_not_steal_focus_from_a_filter_input(qtbot, panel: ExaminePanel, monkeypatch) -> None:
    """A campaign switch reloads rows, but focus belongs to whatever the user
    is typing in, not the table."""
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(panel.ui.detections_table, "_present", lambda *a, **k: None)
    panel.show()
    qtbot.waitExposed(panel)
    edit = panel.ui.detections_table._filter_row._slots[panel._model.index_of("ARU")].edit
    edit.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is edit)

    panel.ui.campaign_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: panel._model.rowCount() == 3)

    assert QApplication.focusWidget() is edit
    QCoreApplication.processEvents()


def test_edit_then_filter_sees_new_value(panel: ExaminePanel) -> None:
    col = panel._model.index_of("Verified")
    assert panel._model.setData(panel._model.index(0, col), "true")
    panel._model.set_column_filter(col, "true", FilterOp.EQUALS)
    assert panel._model.rowCount() == 1


def test_edit_then_sort_sees_new_value(panel: ExaminePanel) -> None:
    col = panel._model.index_of("Comment")
    last = panel._model.rowCount() - 1
    target = panel._model.row_id_at(last)
    assert panel._model.setData(panel._model.index(last, col), "aaa")
    panel._model.sort_by_priority([(col, Qt.DescendingOrder)])
    assert panel._model.row_id_at(0) == target


def test_reload_does_not_reselect_an_unrelated_row(qtbot, panel: ExaminePanel) -> None:
    """Row ids restart in every new store, so a reload must not match on id alone."""
    table = panel.ui.detections_table
    view = table.table()
    view.setCurrentIndex(view.model().index(3, 1))
    picked = panel._model.row_id_at(table._table.mapToSourceRow(3))
    # Loading already selected row 0, so wait for the deferred prepare of row 3.
    qtbot.waitUntil(lambda: table._current_row is not None and table._current_row[1] == picked, timeout=2000)

    panel._on_campaign_selected(panel.ui.campaign_combo.currentIndex())
    QCoreApplication.processEvents()

    assert view.currentIndex().row() == 0


def test_context_detections_are_other_visible_rows_in_the_same_file(panel: ExaminePanel) -> None:
    model = panel._model
    row_id, current = model.row_id_at(0), model.detection_at(0)
    ctx = panel.ui.detections_table._context_detections_for(row_id, current)
    expected = [
        (d.start_time, d.end_time)
        for r, d in enumerate(_visible(panel))
        if d.file == current.file and model.row_id_at(r) != row_id
    ]
    assert sorted((start, end) for start, end, _label in ctx) == sorted(expected)
    assert len(ctx) == 2  # the fixture has 3 rows per campaign, all in f.wav


def _quiet(panel: ExaminePanel, monkeypatch) -> None:
    """Keep row selection from priming the player with the dummy f.wav."""
    monkeypatch.setattr(panel.ui.detections_table, "_present", lambda *a, **k: None)


def _select_rows(panel: ExaminePanel, rows: list[int]) -> None:
    from PySide6.QtCore import QItemSelectionModel

    view = panel.ui.detections_table.table()
    flags = QItemSelectionModel.SelectionFlag
    view.selectionModel().clearSelection()
    for row in rows:
        view.selectionModel().select(view.model().index(row, 0), flags.Select | flags.Rows)


def _row_menu(panel: ExaminePanel, row: int, column: str):
    """The context menu for a right click on *column* of visible *row*."""
    view = panel.ui.detections_table.table()
    return panel.ui.detections_table._build_row_menu(view.model().index(row, panel._model.index_of(column)))


def _menu_action(menu, text: str):
    for action in menu.actions():
        if action.text() == text:
            return action
        if action.menu() is not None:
            found = _menu_action(action.menu(), text)
            if found is not None:
                return found
    return None


def _clipboard() -> str:
    from PySide6.QtWidgets import QApplication

    return QApplication.clipboard().text()


def test_context_menu_copies_the_clicked_cell(panel: ExaminePanel, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    _select_rows(panel, [0])
    _menu_action(_row_menu(panel, 0, "ARU"), "Copy Cell").trigger()
    assert _clipboard() == "MSD-1"


def test_context_menu_copies_the_selected_row_as_tab_separated_text(panel: ExaminePanel, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    _select_rows(panel, [0])
    _menu_action(_row_menu(panel, 0, "ARU"), "Copy Row").trigger()

    fields = _clipboard().split("\t")
    # Default sort puts the highest confidence of alpha / MSD-1 first.
    assert fields[:2] == ["alpha", "MSD-1"]
    assert "0.7" in fields
    assert "\n" not in _clipboard()


def test_context_menu_copies_several_selected_rows(panel: ExaminePanel, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    _select_rows(panel, [0, 1, 3])
    _menu_action(_row_menu(panel, 1, "ARU"), "Copy 3 Rows").trigger()

    lines = _clipboard().split("\n")
    assert [line.split("\t")[:2] for line in lines] == [["alpha", "MSD-1"], ["alpha", "MSD-1"], ["beta", "MSD-2"]]


def test_context_menu_copies_rows_with_a_header_line(panel: ExaminePanel, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    _select_rows(panel, [0])
    _menu_action(_row_menu(panel, 0, "ARU"), "Copy Row with Headers").trigger()

    header, row = (line.split("\t") for line in _clipboard().split("\n"))
    assert header[:2] == ["Campaign", "ARU"]
    assert len(header) == len(row)
    # Hidden by default, and the play column has no content to copy.
    assert "Start_Time" not in header
    assert "" not in header


def test_copied_rows_follow_the_on_screen_column_order_and_visibility(panel: ExaminePanel, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    table = panel.ui.detections_table
    header = table.table().horizontalHeader()
    table._toggle_column(panel._model.index_of("ARU"), False)
    header.moveSection(header.visualIndex(panel._model.index_of("Species")), 1)

    _select_rows(panel, [0])
    _menu_action(_row_menu(panel, 0, "Species"), "Copy Row with Headers").trigger()

    names, row = (line.split("\t") for line in _clipboard().split("\n"))
    assert names[:2] == ["Species", "Campaign"]
    assert row[:2] == ["Robin", "alpha"]
    assert "ARU" not in names


def test_copied_rows_keep_one_line_per_row_when_a_comment_has_tabs_or_newlines(
    panel: ExaminePanel, monkeypatch
) -> None:
    _quiet(panel, monkeypatch)
    panel._model.setData(panel._model.index(0, panel._model.index_of("Comment")), "two\tbirds\nfar away")
    _select_rows(panel, [0])
    _menu_action(_row_menu(panel, 0, "ARU"), "Copy Row with Headers").trigger()

    header, row = (line.split("\t") for line in _clipboard().split("\n"))
    assert len(header) == len(row)
    assert "two birds far away" in row


def test_copy_shortcut_copies_the_selected_rows(qtbot, panel: ExaminePanel, monkeypatch) -> None:
    from PySide6.QtGui import QKeySequence
    from PySide6.QtWidgets import QApplication

    _quiet(panel, monkeypatch)
    panel.show()
    qtbot.waitExposed(panel)
    table = panel.ui.detections_table.table()
    qtbot.waitUntil(lambda: QApplication.focusWidget() in (table, table.viewport()))
    _select_rows(panel, [3, 4])
    QApplication.clipboard().setText("stale")

    qtbot.keySequence(table, QKeySequence(QKeySequence.StandardKey.Copy))

    assert [line.split("\t")[0] for line in _clipboard().split("\n")] == ["beta", "beta"]
    QCoreApplication.processEvents()


def test_context_menu_copies_the_audio_file_path(panel: ExaminePanel, project: Project, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    _select_rows(panel, [0])
    _menu_action(_row_menu(panel, 0, "ARU"), "Copy File Path").trigger()
    assert _clipboard() == str(project.folder / panel._model.detection_at(0).file)


def test_context_menu_opens_the_audio_file_folder(panel: ExaminePanel, project: Project, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    opened: list[Path] = []
    monkeypatch.setattr("pam_analyzer.ui.detection_table.open_in_file_manager", opened.append)
    _select_rows(panel, [0])
    _menu_action(_row_menu(panel, 0, "ARU"), "Open Audio File Folder").trigger()
    assert opened == [(project.folder / panel._model.detection_at(0).file).parent]


def test_open_audio_file_folder_is_disabled_when_the_folder_is_missing(
    panel: ExaminePanel, tmp_path: Path, monkeypatch
) -> None:
    _quiet(panel, monkeypatch)
    panel.ui.detections_table.setAudioRoot(tmp_path / "unplugged")
    _select_rows(panel, [0])
    assert not _menu_action(_row_menu(panel, 0, "ARU"), "Open Audio File Folder").isEnabled()


def test_context_menu_filters_by_the_clicked_value(panel: ExaminePanel, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    col = panel._model.index_of("ARU")
    _menu_action(_row_menu(panel, 4, "ARU"), "Filter by This Value").trigger()

    assert {d.aru for d in _visible(panel)} == {"MSD-2"}
    filter_row = panel.ui.detections_table._filter_row
    # The filter row shows the filter, so the user can see and edit it.
    assert filter_row._slots[col].edit.text() == "MSD-2"
    assert filter_row.column_op(col) is FilterOp.EQUALS
    QCoreApplication.processEvents()


def test_context_menu_filters_a_blank_cell_with_the_blank_operator(panel: ExaminePanel, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    comment = panel._model.index_of("Comment")
    panel._model.setData(panel._model.index(0, comment), "keep")
    _menu_action(_row_menu(panel, 1, "Comment"), "Filter by This Value").trigger()

    assert panel._model.rowCount() == 5
    assert panel.ui.detections_table._filter_row.column_op(comment) is FilterOp.BLANK
    QCoreApplication.processEvents()


def test_context_menu_clears_all_filters(panel: ExaminePanel, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    assert not _menu_action(_row_menu(panel, 0, "ARU"), "Clear All Filters").isEnabled()
    _menu_action(_row_menu(panel, 4, "ARU"), "Filter by This Value").trigger()
    _menu_action(_row_menu(panel, 0, "Species"), "Filter by This Value").trigger()
    assert panel._model.rowCount() == 3

    _menu_action(_row_menu(panel, 0, "ARU"), "Clear All Filters").trigger()

    assert panel._model.rowCount() == 6
    filter_row = panel.ui.detections_table._filter_row
    assert filter_row._slots[panel._model.index_of("ARU")].edit.text() == ""
    assert filter_row.column_op(panel._model.index_of("Species")) is FilterOp.CONTAINS
    QCoreApplication.processEvents()


def test_context_menu_offers_no_cell_actions_on_the_play_column(panel: ExaminePanel, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    _select_rows(panel, [0])
    view = panel.ui.detections_table.table()
    menu = panel.ui.detections_table._build_row_menu(view.model().index(0, 0))
    assert not _menu_action(menu, "Copy Cell").isEnabled()
    assert not _menu_action(menu, "Filter by This Value").isEnabled()
    assert _menu_action(menu, "Copy Row").isEnabled()


def test_context_menu_below_the_last_row_only_offers_clearing_filters(panel: ExaminePanel) -> None:
    from PySide6.QtCore import QModelIndex

    menu = panel.ui.detections_table._build_row_menu(QModelIndex())
    assert [a.text() for a in menu.actions()] == ["Clear All Filters"]


def test_context_menu_marks_all_selected_rows(panel: ExaminePanel, monkeypatch) -> None:
    from pam_analyzer.domain import VerifiedState

    _quiet(panel, monkeypatch)
    _select_rows(panel, [0, 2])
    _menu_action(_row_menu(panel, 0, "ARU"), "True").trigger()
    assert [d.verified for d in _visible(panel)[:3]] == [VerifiedState.TRUE, VerifiedState.UNSET, VerifiedState.TRUE]

    _menu_action(_row_menu(panel, 0, "ARU"), "Unset").trigger()
    assert {d.verified for d in _visible(panel)} == {VerifiedState.UNSET}


def test_verified_shortcut_marks_all_selected_rows(panel: ExaminePanel, monkeypatch) -> None:
    from pam_analyzer.domain import VerifiedState

    _quiet(panel, monkeypatch)
    _select_rows(panel, [1, 2])
    panel.ui.detections_table._set_verified("false")
    assert [d.verified for d in _visible(panel)[:3]] == [VerifiedState.UNSET, VerifiedState.FALSE, VerifiedState.FALSE]


def test_context_menu_exports_only_the_selected_rows_as_snippets(
    panel: ExaminePanel, project: Project, tmp_path: Path, monkeypatch, toasts
) -> None:
    _quiet(panel, monkeypatch)
    _prepare_snippet_export(panel, project, tmp_path, monkeypatch)
    starts: list[float] = []
    monkeypatch.setattr(
        "pam_analyzer.ui.panels.examine_panel.extract_snippet", lambda _src, start, _end, _dst: starts.append(start)
    )
    _select_rows(panel, [0, 2])
    _menu_action(_row_menu(panel, 0, "ARU"), "Export Selected as Audio Snippets…").trigger()

    assert starts == [panel._model.detection_at(0).start_time, panel._model.detection_at(2).start_time]
    [(kind, _title, text, _kwargs)] = toasts
    assert kind == "success"
    assert "2 snippets" in text


def test_right_click_selects_an_unselected_row_but_keeps_a_multi_selection(
    qtbot, panel: ExaminePanel, monkeypatch
) -> None:
    _quiet(panel, monkeypatch)
    panel.show()
    qtbot.waitExposed(panel)
    view = panel.ui.detections_table.table()
    aru = panel._model.index_of("ARU")

    def right_click(row: int) -> None:
        pos = view.visualRect(view.model().index(row, aru)).center()
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=pos)

    right_click(4)
    assert panel.ui.detections_table.selectedRows() == [4]

    _select_rows(panel, [1, 2, 4])
    right_click(2)
    assert panel.ui.detections_table.selectedRows() == [1, 2, 4]
    QCoreApplication.processEvents()


def test_marking_several_rows_emits_one_data_change(panel: ExaminePanel, monkeypatch) -> None:
    _quiet(panel, monkeypatch)
    _select_rows(panel, [0, 2, 3])
    verified = panel._model.index_of("Verified")
    changes: list[tuple[int, int, int, int]] = []
    panel._model.dataChanged.connect(
        lambda top_left, bottom_right, _roles: changes.append(
            (top_left.row(), top_left.column(), bottom_right.row(), bottom_right.column())
        )
    )

    panel.ui.detections_table._set_verified("true")

    assert changes == [(0, verified, 3, verified)]
    assert [d.verified.value for d in _visible(panel)][:4] == ["true", "", "true", "true"]
    assert panel._detections.dirty_count == 3


def test_model_set_annotation_rejects_bad_input(panel: ExaminePanel) -> None:
    assert not panel._model.set_annotation([0, 1], "Verified", "maybe")
    assert not panel._model.set_annotation([0, 1], "Species", "Crow")
    assert not panel._model.set_annotation([], "Verified", "true")
    assert not panel._model.set_annotation([0, 99], "Verified", "true")
    assert panel._detections.dirty_count == 0


def test_selected_rows_after_select_all_does_not_query_every_cell(qtbot, panel: ExaminePanel, monkeypatch) -> None:
    from pam_analyzer.ui.detection_table import DetectionTable
    from pam_analyzer.ui.models.detections_table_model import DetectionsTableModel

    class CountingModel(DetectionsTableModel):
        flags_calls = 0

        def flags(self, index):
            self.flags_calls += 1
            return super().flags(index)

    model = CountingModel()
    model.set_store(panel._model.store)
    table = DetectionTable()
    qtbot.addWidget(table)
    monkeypatch.setattr(table, "_present", lambda *a, **k: None)
    table.setModel(model)
    table.table().selectAll()
    model.flags_calls = 0

    assert table.selectedRows() == list(range(model.rowCount()))
    menu = table._build_row_menu(table.table().model().index(0, 1))
    assert f"Copy {model.rowCount()} Rows" in [action.text() for action in menu.actions()]
    assert model.flags_calls < model.rowCount()


def test_right_click_position_opens_the_menu_for_the_cell_under_it(qtbot, panel: ExaminePanel, monkeypatch) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMenu

    _quiet(panel, monkeypatch)
    panel.show()
    qtbot.waitExposed(panel)
    view = panel.ui.detections_table.table()
    _select_rows(panel, [1])
    seen: list[str] = []

    def _copy_cell_and_close() -> None:
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        seen.extend(a.text() for a in menu.actions())
        _menu_action(menu, "Copy Cell").trigger()
        menu.close()

    # QMenu.exec blocks, so the popup is driven from a single-shot timer.
    QTimer.singleShot(0, _copy_cell_and_close)
    view.customContextMenuRequested.emit(view.visualRect(view.model().index(1, panel._model.index_of("ARU"))).center())

    assert "Export Selected as Audio Snippets…" in seen
    assert _clipboard() == "MSD-1"
    QCoreApplication.processEvents()
