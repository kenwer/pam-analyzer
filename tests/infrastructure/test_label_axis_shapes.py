"""The label-shape fact the species-list reader is built on.

A list line is read against the running model's own axis: a line that already
names one of its classes is that class, and anything else is a BirdNET
'Scientific_Common' entry that loses its common-name half. That is only sound
while no BirdNET list entry is spelled like a Perch label, because such a line
would be kept whole on a Perch run and then match nothing the model emits.

The label files are upstream data, so the property is asserted here instead of
assumed: a file that changed shape should fail loudly rather than quietly
mis-read a list.

Marked slow like the other tests that reach the model's label files.
"""

from __future__ import annotations

import pytest

from pam_analyzer.infrastructure.birdnet_lib import TAXONOMY_V2_4, TAXONOMY_V3_0
from pam_analyzer.infrastructure.perch_onnx import label_set


@pytest.mark.slow
@pytest.mark.parametrize("taxonomy", [TAXONOMY_V2_4, TAXONOMY_V3_0], ids=["v2.4", "v3.0"])
def test_no_birdnet_list_entry_is_spelled_like_a_perch_label(taxonomy) -> None:  # noqa: ANN001
    """A user's list carries whole 'Scientific_Common' entries, which is the
    only shape a Perch run could mistake for one of its own class names. The
    bare scientific name a user may equally have typed needs no such check: if
    it is a Perch class, keeping it whole is the right reading.
    """
    labels = taxonomy.locale_label_map("en_us")
    assert labels, "label read failed, the map came back empty"

    entries = {f"{sci}_{common}" for sci, common in labels.items()}
    assert entries & label_set() == set()
