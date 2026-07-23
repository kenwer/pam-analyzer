"""Smoke tests for the Project settings panel, covering the analysis knobs
(min confidence, overlap, output languages) that moved here from the BirdNET
panel to become project-wide settings."""

from pathlib import Path

import pytest

from pam_analyzer.domain import Project
from pam_analyzer.infrastructure import TomlCampaignRepository, TomlProjectRepository
from pam_analyzer.ui.app_state import AppState
from pam_analyzer.ui.panels.project_panel import ProjectPanel

_LOCALES = ("de", "en_uk", "en_us", "fr")


@pytest.fixture
def state() -> AppState:
    return AppState(TomlProjectRepository(), TomlCampaignRepository())


@pytest.fixture
def loaded_project(tmp_path: Path) -> Project:
    proj = Project(folder=tmp_path / "proj")
    TomlProjectRepository().save(proj)
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


def test_controls_reflect_loaded_project(qtbot, state: AppState, tmp_path: Path):
    proj = Project(folder=tmp_path / "p", min_conf=0.6, overlap=1.2, locales=("fr",))
    TomlProjectRepository().save(proj)
    p = _panel(qtbot, state)
    state.load_project(proj.folder)

    assert p.ui.min_conf_slider.value() == 60
    assert p.ui.overlap_slider.value() == 12
    assert p.ui.min_conf_value.text() == "0.60"
    assert p._locale_checks["fr"].isChecked()
    assert not p._locale_checks["de"].isChecked()


def test_slider_autosave_persists_to_project(qtbot, state: AppState, loaded_project: Project):
    p = _panel(qtbot, state)
    state.load_project(loaded_project.folder)

    p.ui.min_conf_slider.setValue(60)
    assert state.project is not None
    assert abs(state.project.min_conf - 0.60) < 1e-9
    # Persisted to disk, not just held in memory.
    assert abs(TomlProjectRepository().load(loaded_project.folder).min_conf - 0.60) < 1e-9


def test_locale_selection_persists_to_project(qtbot, state: AppState, loaded_project: Project):
    p = _panel(qtbot, state)
    state.load_project(loaded_project.folder)

    p._locale_checks["de"].setChecked(True)
    assert state.project is not None
    assert state.project.locales == ("de",)


def test_main_combo_uses_model_locales(qtbot, state: AppState, tmp_path: Path):
    """Main and Extra draw from the same model locale list: no bare 'en', and a
    legacy stored 'en' displays as its canonical 'en_us'."""
    proj = Project(folder=tmp_path / "p", preferred_species_lang="en")
    TomlProjectRepository().save(proj)
    p = _panel(qtbot, state)
    state.load_project(proj.folder)

    combo = p.ui.species_lang_combo
    items = [combo.itemText(i) for i in range(combo.count())]
    assert "en" not in items
    assert {"en_uk", "en_us"} <= set(items)
    assert combo.currentText() == "en_us"


def test_overlap_slider_capped_at_max(qtbot, state: AppState, loaded_project: Project):
    """The conservative cross-model cap (2.9 s) bounds the slider."""
    p = _panel(qtbot, state)
    state.load_project(loaded_project.folder)

    p.ui.overlap_slider.setValue(999)  # clamps to the slider maximum
    assert state.project is not None
    assert state.project.overlap <= 2.9
