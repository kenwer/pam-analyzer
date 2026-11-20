"""The engines' model packages follow their upstream pins, with no network."""

from birdnet.acoustic.models.v3_0.onnx import AcousticOnnxDownloaderV3_0
from birdnet.acoustic.models.v3_0.onnx import models as v3_acoustic_models
from birdnet.geo.models.v3_0.onnx import GeoOnnxDownloaderV3_0
from birdnet.geo.models.v3_0.onnx import models as v3_geo_models
from birdnet.utils.taxonomy_v3 import get_taxonomy_v3_input

from pam_analyzer.infrastructure import perch_onnx
from pam_analyzer.infrastructure.birdnet_2_4_runner import Birdnet24Runner
from pam_analyzer.infrastructure.birdnet_3_0_runner import Birdnet30Runner
from pam_analyzer.infrastructure.birdnet_3_0_taxonomy import geo_artifact, label_artifacts, prepare_labels
from pam_analyzer.infrastructure.perch_runner import PerchRunner

PERCH_COMMIT_URL_PREFIX = "https://huggingface.co/justinchuby/Perch-onnx/resolve/"
PERCH_SHA256 = "4dcf71c18a147198545944bb5149697e89e3ad2e16637fa8f0edf6d13035a017"


def test_v3_0_artifacts_follow_lib_pins() -> None:
    for artifact, info in (
        (next(a for a in Birdnet30Runner.package.artifacts() if a.id == "birdnet-3.0-acoustic"), v3_acoustic_models["fp32"]),
        (geo_artifact(), v3_geo_models["fp32"]),
    ):
        assert artifact.url == info.dl_url
        assert artifact.sha256 == info.sha256
        assert artifact.size == info.file_size
        assert info.content_tag in artifact.dest.name
        assert artifact.stale_glob == "model-fp32*.onnx"


def test_v3_0_label_inputs_download_to_the_lib_paths() -> None:
    """The lib then finds its inputs in place and only generates the locale files."""
    inputs = (
        AcousticOnnxDownloaderV3_0._labels_input(),
        GeoOnnxDownloaderV3_0._labels_input(),
        get_taxonomy_v3_input(),
    )
    assert {(a.dest, a.url, a.size, a.sha256) for a in label_artifacts()} == {
        (i.path, i.url, i.size, i.sha256) for i in inputs
    }


def test_perch_artifact_is_pinned_to_a_commit() -> None:
    artifact = perch_onnx.perch_artifact()
    assert artifact.url.startswith(PERCH_COMMIT_URL_PREFIX)
    assert artifact.sha256 == PERCH_SHA256
    assert artifact.dest == perch_onnx.model_path()
    assert artifact.size > 400_000_000


def test_v3_0_and_perch_share_geo_and_label_files() -> None:
    shared = {a.id for a in (geo_artifact(), *label_artifacts())}
    assert shared <= {a.id for a in Birdnet30Runner.package.artifacts()}
    assert shared <= {a.id for a in PerchRunner.package.artifacts()}


def test_v2_4_reads_the_v3_0_labels_for_its_common_names() -> None:
    labels = {a.id for a in label_artifacts()}
    assert labels <= {a.id for a in Birdnet24Runner.package.artifacts()}


def test_v3_0_and_perch_generate_labels_after_download() -> None:
    assert Birdnet30Runner.package.prepare is prepare_labels
    assert PerchRunner.package.prepare is prepare_labels
