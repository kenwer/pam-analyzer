"""discover_analysis_inventory: the on-disk view of earlier runs the Analyze panel lists."""

from pathlib import Path

from pam_analyzer.domain import detection_schema as schema
from pam_analyzer.domain import paths
from pam_analyzer.infrastructure.analysis_inventory_discovery import discover_analysis_inventory
from tests.conftest import ALT_MODEL_KEY, DEFAULT_MODEL_KEY


def _campaign(project: Path, name: str) -> Path:
    folder = project / name
    folder.mkdir()
    paths.campaign_toml(folder).write_text("", encoding="utf-8")
    return folder


def test_one_entry_per_model_csv_with_its_species_list(tmp_path: Path) -> None:
    east = _campaign(tmp_path, "east")
    schema.campaign_csv_for_model(east, DEFAULT_MODEL_KEY).write_text("Species\nRobin\nCrow\n", encoding="utf-8")
    schema.campaign_csv_for_model(east, ALT_MODEL_KEY).write_text("Species\nRobin\n", encoding="utf-8")
    paths.applied_species_list_file(east).write_text("Robin\n", encoding="utf-8")
    _campaign(tmp_path, "west")  # never analysed

    inventory = discover_analysis_inventory(tmp_path)

    assert inventory is not None
    by_model = {e.model_key: e for e in inventory.campaigns}
    assert set(by_model) == {DEFAULT_MODEL_KEY, ALT_MODEL_KEY}
    assert by_model[DEFAULT_MODEL_KEY].detection_count == 2
    assert by_model[ALT_MODEL_KEY].detection_count == 1
    assert all(e.campaign_name == "east" for e in inventory.campaigns)
    assert all(e.species_list_txt == paths.applied_species_list_file(east) for e in inventory.campaigns)


def test_a_project_without_detection_csvs_has_no_inventory(tmp_path: Path) -> None:
    _campaign(tmp_path, "east")
    assert discover_analysis_inventory(tmp_path) is None


def test_a_comment_spanning_lines_counts_as_one_detection(tmp_path: Path) -> None:
    """The Examine grid saves multi-line comments as quoted CSV fields."""
    east = _campaign(tmp_path, "east")
    schema.campaign_csv_for_model(east, DEFAULT_MODEL_KEY).write_text(
        'Species,Comment\r\nRobin,"first line\nsecond line"\r\nCrow,\r\n', encoding="utf-8", newline=""
    )

    inventory = discover_analysis_inventory(tmp_path)
    assert inventory is not None
    (entry,) = inventory.campaigns

    assert entry.detection_count == 2
