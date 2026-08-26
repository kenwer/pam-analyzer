"""Smoke tests for the Project settings panel, covering the analysis knobs
(min confidence, overlap, output languages) that moved here from the BirdNET
panel to become project-wide settings."""

from pathlib import Path

import pytest

from pam_analyzer.domain import DEFAULT_SPECIES_LANG, Project
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.panels.project_panel import ProjectPanel

_LOCALES = ("de", "en_uk", "en_us", "fr")


@pytest.fixture
def state() -> AppState:
    return AppState()


@pytest.fixture
def loaded_project(tmp_path: Path) -> Project:
    proj = Project(folder=tmp_path / "proj")
    proj.save()
    return proj


def _panel(qtbot, state: AppState) -> ProjectPanel:
    p = ProjectPanel(state, _LOCALES)
    qtbot.addWidget(p)
    return p


def test_settings_disabled_without_project(qtbot, state: AppState):
    p = _panel(qtbot, state)
    assert not p.ui.min_conf_slider.isEnabled()
    assert not p.ui.overlap_slider.isEnabled()
    assert not p._locale_checks["de"].isEnabled()


def test_controls_reflect_loaded_project(qtbot, state: AppState, tmp_path: Path, load_project):
    proj = Project(folder=tmp_path / "p", min_conf=0.6, overlap=1.2, locales=("fr",))
    proj.save()
    p = _panel(qtbot, state)
    load_project(state, proj.folder)

    assert p.ui.min_conf_slider.value() == 60
    assert p.ui.overlap_slider.value() == 12
    assert p.ui.min_conf_value.text() == "0.60"
    assert p._locale_checks["fr"].isChecked()
    assert not p._locale_checks["de"].isChecked()


def test_slider_autosave_persists_to_project(
    qtbot, state: AppState, loaded_project: Project, load_project
):
    p = _panel(qtbot, state)
    load_project(state, loaded_project.folder)

    p.ui.min_conf_slider.setValue(60)
    assert state.project is not None
    assert abs(state.project.min_conf - 0.60) < 1e-9
    # Persisted to disk, not just held in memory.
    assert abs(Project.load(loaded_project.folder).min_conf - 0.60) < 1e-9


def test_locale_selection_persists_to_project(
    qtbot, state: AppState, loaded_project: Project, load_project
):
    p = _panel(qtbot, state)
    load_project(state, loaded_project.folder)

    p._locale_checks["de"].setChecked(True)
    assert state.project is not None
    assert state.project.locales == ("de",)


def test_main_combo_uses_model_locales(qtbot, state: AppState, tmp_path: Path, load_project):
    """Main and Extra draw from the same model locale list: no bare 'en', and a
    legacy stored 'en' displays as its canonical 'en_us'."""
    proj = Project(folder=tmp_path / "p", preferred_species_lang="en")
    proj.save()
    p = _panel(qtbot, state)
    load_project(state, proj.folder)

    combo = p.ui.species_lang_combo
    items = [combo.itemText(i) for i in range(combo.count())]
    assert "en" not in items
    assert {"en_uk", "en_us"} <= set(items)
    assert combo.currentText() == "en_us"


def test_overlap_slider_capped_at_max(
    qtbot, state: AppState, loaded_project: Project, load_project
):
    """The conservative cross-model cap (2.9 s) bounds the slider."""
    p = _panel(qtbot, state)
    load_project(state, loaded_project.folder)

    p.ui.overlap_slider.setValue(999)  # clamps to the slider maximum
    assert state.project is not None
    assert state.project.overlap <= 2.9


def test_main_combo_is_closed(qtbot, state: AppState):
    """Typing is off, so a language outside the offered list cannot come back
    in through the line edit after the panel filtered it out."""
    p = _panel(qtbot, state)
    assert not p.ui.species_lang_combo.isEditable()


def test_unsupported_main_language_is_corrected_on_disk_not_just_on_screen(
    qtbot, state: AppState, tmp_path: Path, load_project
):
    """A project naming a language not every model ships falls back to en_us.

    Saved rather than only displayed: leaving the combo saying en_us while
    the next run wrote Italian names would make the panel lie about the CSV.
    """
    proj = Project(folder=tmp_path / "p", preferred_species_lang="it")
    proj.save()
    p = _panel(qtbot, state)
    load_project(state, proj.folder)

    assert p.ui.species_lang_combo.currentText() == DEFAULT_SPECIES_LANG
    assert Project.load(proj.folder).preferred_species_lang == DEFAULT_SPECIES_LANG


def test_unsupported_extra_locales_are_dropped_on_disk(
    qtbot, state: AppState, tmp_path: Path, load_project
):
    proj = Project(folder=tmp_path / "p", locales=("it", "de"))
    proj.save()
    p = _panel(qtbot, state)
    load_project(state, proj.folder)

    assert p._locale_checks["de"].isChecked()
    assert Project.load(proj.folder).locales == ("de",)


def test_supported_extra_locales_are_left_alone(
    qtbot, state: AppState, tmp_path: Path, load_project
):
    """No rewrite when every stored locale is offered."""
    proj = Project(folder=tmp_path / "p", locales=("de", "fr"))
    proj.save()
    _panel(qtbot, state)
    load_project(state, proj.folder)

    assert Project.load(proj.folder).locales == ("de", "fr")
