"""Tests for load_project_bundle: the single read sequence shared by AppState's
synchronous load_project and the async ProjectLoadWorker."""

from pathlib import Path

import pytest

from pam_analyzer.infrastructure import (
    TomlCampaignRepository,
    TomlProjectRepository,
    load_project_bundle,
)


def test_bundles_project_campaigns_and_inventory(tmp_path: Path) -> None:
    project_repo = TomlProjectRepository()
    campaign_repo = TomlCampaignRepository()
    project_repo.create(tmp_path)
    campaign_dir = tmp_path / "alpha"
    campaign_dir.mkdir()
    (campaign_dir / "campaign.toml").write_text('species_filter_mode = "location"\n', encoding="utf-8")

    result = load_project_bundle(project_repo, campaign_repo, tmp_path)

    assert result.project.folder == tmp_path
    assert [c.name for c in result.campaigns] == ["alpha"]
    (inventory_campaign,) = result.audio_inventory.campaigns
    assert inventory_campaign.name == "alpha"
    assert inventory_campaign.file_count == 0  # no audio imported into 'alpha' yet
    assert result.analysis_result is None  # no analysis has been run yet


def test_missing_project_toml_raises(tmp_path: Path) -> None:
    project_repo = TomlProjectRepository()
    campaign_repo = TomlCampaignRepository()

    with pytest.raises(FileNotFoundError):
        load_project_bundle(project_repo, campaign_repo, tmp_path)
