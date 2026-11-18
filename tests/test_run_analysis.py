"""scripts/run_analysis.py selects campaigns by the names typed on the command line."""

import unicodedata
from pathlib import Path

from pam_analyzer.domain import Campaign
from scripts.run_analysis import _select_campaigns


def test_a_campaign_name_matches_in_either_unicode_form(tmp_path: Path) -> None:
    """macOS hands folder names back decomposed (NFD), while a typed argument is composed (NFC)."""
    folder = tmp_path / unicodedata.normalize("NFD", "TüCamp1")
    folder.mkdir()
    Campaign(name=folder.name, folder=folder).save()

    selected = _select_campaigns(tmp_path, [unicodedata.normalize("NFC", "TüCamp1")])

    assert selected is not None
    assert [c.folder for c in selected] == [folder]
