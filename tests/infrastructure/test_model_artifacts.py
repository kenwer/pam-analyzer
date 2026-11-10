"""The engines' model packages follow their upstream pins, with no network."""

from birdnet.acoustic.models.v3_0.onnx import AcousticOnnxDownloaderV3_0
from birdnet.acoustic.models.v3_0.onnx import models as v3_acoustic_models
from birdnet.geo.models.v3_0.onnx import GeoOnnxDownloaderV3_0
from birdnet.geo.models.v3_0.onnx import models as v3_geo_models
from birdnet.utils.taxonomy_v3 import get_taxonomy_v3_input

from pam_analyzer.infrastructure import perch_onnx
from pam_analyzer.infrastructure.birdnet_2_4_runner import Birdnet24Runner
from pam_analyzer.infrastructure.birdnet_lib import (
    prepare_v3_0_labels,
    v3_0_artifacts,
    v3_0_geo_artifact,
    v3_0_label_artifacts,
)
from pam_analyzer.infrastructure.birdnet_runner import BirdnetRunner
from pam_analyzer.infrastructure.perch_runner import PerchRunner

PERCH_COMMIT_URL_PREFIX = "https://huggingface.co/justinchuby/Perch-onnx/resolve/"
PERCH_SHA256 = "4dcf71c18a147198545944bb5149697e89e3ad2e16637fa8f0edf6d13035a017"


def test_v3_0_artifacts_follow_lib_pins() -> None:
    for artifact, info in (
        (next(a for a in v3_0_artifacts() if a.id == "birdnet-3.0-acoustic"), v3_acoustic_models["fp32"]),
        (v3_0_geo_artifact(), v3_geo_models["fp32"]),
    ):
        assert artifact.url == info.dl_url
        assert artifact.sha256 == info.sha256
        assert artifact.size == info.file_size
        assert info.content_tag in artifact.dest.name
        assert artifact.stale_glob == "model-fp32*.onnx"


def test_v3_0_label_inputs_download_to_the_lib_paths() -> None:
    """The lib then finds its inputs in place and only generates the locale files."""
    inputs = (
        AcousticOnnxDownloaderV3_0._labels_input(),  # noqa: SLF001
        GeoOnnxDownloaderV3_0._labels_input(),  # noqa: SLF001
        get_taxonomy_v3_input(),
    )
    assert {(a.dest, a.url, a.size, a.sha256) for a in v3_0_label_artifacts()} == {
        (i.path, i.url, i.size, i.sha256) for i in inputs
    }


def test_perch_artifact_is_pinned_to_a_commit() -> None:
    artifact = perch_onnx.perch_artifact()
    assert artifact.url.startswith(PERCH_COMMIT_URL_PREFIX)
    assert artifact.sha256 == PERCH_SHA256
    assert artifact.dest == perch_onnx.model_path()
    assert artifact.size > 400_000_000


def test_v3_0_and_perch_share_geo_and_label_files() -> None:
    shared = {a.id for a in (v3_0_geo_artifact(), *v3_0_label_artifacts())}
    assert shared <= {a.id for a in BirdnetRunner.package.artifacts()}
    assert shared <= {a.id for a in PerchRunner.package.artifacts()}


def test_v3_0_and_perch_generate_labels_after_download() -> None:
    assert BirdnetRunner.package.prepare is prepare_v3_0_labels
    assert PerchRunner.package.prepare is prepare_v3_0_labels


def test_model_info_licences() -> None:
    assert Birdnet24Runner.package.info.licence == "CC BY-NC-SA 4.0"
    assert BirdnetRunner.package.info.licence == "CC BY-NC-SA 4.0"
    assert PerchRunner.package.info.licence == "Apache 2.0"


def test_model_info_links() -> None:
    assert Birdnet24Runner.package.info.info_url == "https://zenodo.org/records/15050749"
    assert BirdnetRunner.package.info.info_url == "https://zenodo.org/records/20703646"
    assert PerchRunner.package.info.info_url == "https://huggingface.co/justinchuby/Perch-onnx"
