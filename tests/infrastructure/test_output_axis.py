"""Which class axis each runner reports as its own.

BaseAnalysisRunner uses it twice: to tell a geography drop apart from a name
the model cannot emit at all, and to read a species-list line. Both need the
axis of the model that actually ran, which for PerchRunner is not the BirdNET
v3.0 axis it borrows for geo lookups and locale labels.

Marked slow like the other tests that reach the model's label files.
"""

from __future__ import annotations

import pytest

from pam_analyzer.infrastructure.birdnet_2_4_runner import Birdnet24Runner
from pam_analyzer.infrastructure.birdnet_runner import BirdnetRunner
from pam_analyzer.infrastructure.perch_runner import PerchRunner


@pytest.mark.slow
def test_perch_reports_its_own_axis_not_the_borrowed_v3_one() -> None:
    """PerchRunner binds TAXONOMY_V3_0 for geo lookups and locale maps, but its
    model emits thousands of classes v3.0 has no name for. Reporting v3.0's
    axis here would file every one of them as a name the model cannot emit.
    """
    axis = PerchRunner()._known_output_species()

    assert "Acoustic_guitar" in axis
    assert "Macaca mulatta" in axis
    assert "Turdus merula" in axis
    assert axis != PerchRunner().taxonomy.known_species_scientific()


@pytest.mark.slow
def test_a_birdnet_runner_reports_its_own_taxonomy() -> None:
    assert BirdnetRunner()._known_output_species() == BirdnetRunner().taxonomy.known_species_scientific()
    assert "Acoustic_guitar" not in BirdnetRunner()._known_output_species()


@pytest.mark.slow
def test_the_two_birdnet_generations_report_different_axes() -> None:
    """One shared lookup would hand a v2.4 run v3.0's axis."""
    v3 = BirdnetRunner()._known_output_species()
    v2 = Birdnet24Runner()._known_output_species()

    assert v3 != v2
    assert "Astur gentilis" in v3
    assert "Accipiter gentilis" in v2


@pytest.mark.slow
def test_known_species_scientific_does_not_rebuild_its_set_per_call() -> None:
    """The row loop asks per dropped detection. Rebuilding a set of thousands
    of names each time cost minutes on a large campaign, and the label map
    underneath was already cached, so the cost hid one layer down.
    """
    taxonomy = BirdnetRunner().taxonomy

    assert taxonomy.known_species_scientific() is taxonomy.known_species_scientific()


@pytest.mark.slow
def test_perch_does_not_hand_out_the_label_set_the_model_holds() -> None:
    """labels() is cached for the process and the same object is handed to
    AcousticModelPerchV2.load, so returning it here would put the live model's
    label-to-index mapping within reach of any caller. Shifting that mapping
    would mis-name every subsequent detection without raising anything.
    """
    from pam_analyzer.infrastructure import perch_onnx

    axis = PerchRunner()._known_output_species()

    assert axis is not perch_onnx.labels()
    assert not hasattr(axis, "add"), "a mutable axis invites exactly that mistake"
    assert axis == frozenset(perch_onnx.labels()), "narrowing must not drop names"
