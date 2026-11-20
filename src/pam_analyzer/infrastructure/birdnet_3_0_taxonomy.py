"""BirdNET v3.0's taxonomy: its label files, its geo model and the files behind them.

Everything here is what TaxonomyServices reads for v3.0. The acoustic weights
are not, and belong to birdnet_3_0_runner. Perch and BirdNET v2.4 reuse parts
of this module, Perch for its geo filter and common names, v2.4 for its common
names only, so their packages download the files listed here too.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cached_property
from typing import Any, Literal

from .birdnet_taxonomy import MODEL_PRECISION, TaxonomyServices
from .model_store import Artifact
from .species_names import canonical_set

# The version the lib loads and lays out its app-data directory under, for
# the acoustic and the geo model alike.
VERSION = "3.0"

# The taxon_group values BirdNET+ Taxonomy gives classes that are a sound
# rather than an animal (engine, siren, rain, speech). Read from the raw v3.0
# label file here and from the taxonomy CSV by scripts/build_species_aliases.py.
SOUND_CLASS_GROUPS = frozenset({"Anthropogenic", "Geophony", "Human", "Music", "Soundscape"})


def _downloader(kind: Literal["acoustic", "geo"]):  # noqa: ANN202
    if kind == "acoustic":
        from birdnet.acoustic.models.v3_0.onnx import AcousticOnnxDownloaderV3_0

        return AcousticOnnxDownloaderV3_0
    from birdnet.geo.models.v3_0.onnx import GeoOnnxDownloaderV3_0

    return GeoOnnxDownloaderV3_0


def labels(lang: str, kind: Literal["acoustic", "geo"] = "acoustic"):  # noqa: ANN201
    """One BirdNET v3.0 model's 'Scientific_Common' entries for one locale, in output order.

    Label files only: the lib generates them from its label and taxonomy
    downloads on first use, and the model weights are never touched.
    """
    import birdnet
    from birdnet.utils.species_lists import read_official_labels

    downloader = _downloader(kind)
    model = birdnet.AcousticModelV3_0 if kind == "acoustic" else birdnet.GeoModelV3_0
    downloader.ensure_labels_available()
    return read_official_labels(downloader.get_lang_file(lang), model.get_n_classes(), f"{kind} model v3.0")


class _Taxonomy(TaxonomyServices):
    def available_locales(self) -> tuple[str, ...]:
        import birdnet

        return tuple(sorted(birdnet.VALID_MODEL_LANGUAGES_V3_0))

    def _label_entries(self, lang: str) -> Sequence[str]:
        return labels(lang)

    def _load_geo_model(self) -> Any:
        """Loading triggers a one-time download into the lib's app-data
        directory (default ~/Library/Application Support/birdnet on macOS,
        overridable via BIRDNET_APP_DATA).
        """
        import birdnet

        from .onnx_telemetry import disable_in_workers

        return disable_in_workers(birdnet.load("geo", VERSION, "onnx", lang="en_us", precision=MODEL_PRECISION))

    @cached_property
    def _sound_class_names(self) -> frozenset[str]:
        """Read from the raw label file, the only one that carries taxon_group."""
        import csv

        downloader = _downloader("acoustic")
        downloader.ensure_labels_available()
        path = downloader._labels_input().path  # noqa: SLF001
        with open(path, encoding="utf-8-sig", newline="") as f:
            return canonical_set(
                row["scientific_name"].strip() for row in csv.DictReader(f) if row["taxon_group"] in SOUND_CLASS_GROUPS
            )

    def _sound_classes(self) -> frozenset[str]:
        return self._sound_class_names


TAXONOMY: TaxonomyServices = _Taxonomy()


def model_artifact(kind: Literal["acoustic", "geo"]) -> Artifact:
    """BirdNET v3.0's acoustic or geo weights, pinned by the lib, at the path the lib looks in.

    Serves the acoustic model too, so the runner's download follows the same
    lib pins as the geo model here.
    """
    from birdnet.utils.local_data import get_model_path

    if kind == "acoustic":
        from birdnet.acoustic.models.v3_0.onnx import models
    else:
        from birdnet.geo.models.v3_0.onnx import models
    info = models[MODEL_PRECISION]
    return Artifact(
        id=f"birdnet-3.0-{kind}",
        url=info.dl_url,
        size=info.file_size,
        sha256=info.sha256,
        dest=get_model_path(kind, VERSION, "onnx", MODEL_PRECISION, content_tag=info.content_tag),
        stale_glob=f"model-{MODEL_PRECISION}*.onnx",
    )


def geo_artifact() -> Artifact:
    """BirdNET v3.0's geo model, also the location filter of Perch runs."""
    return model_artifact("geo")


def label_artifacts() -> tuple[Artifact, ...]:
    """The raw label files and the taxonomy the v3.0 locale files are generated from.

    Downloaded to the paths the lib reads, so prepare_labels() finds them
    verified in place and fetches nothing itself. _labels_input() is private
    birdnet API, pinned by test_v3_0_label_inputs_download_to_the_lib_paths.
    """
    from birdnet.utils.taxonomy_v3 import get_taxonomy_v3_input

    inputs = {
        "birdnet-3.0-acoustic-labels": _downloader("acoustic")._labels_input(),  # noqa: SLF001
        "birdnet-3.0-geo-labels": _downloader("geo")._labels_input(),  # noqa: SLF001
        "birdnet-3.0-taxonomy": get_taxonomy_v3_input(),
    }
    return tuple(
        Artifact(id=key, url=i.url, size=i.size, sha256=i.sha256, dest=i.path) for key, i in inputs.items()
    )


def prepare_labels() -> None:
    """Generate the per-locale label files of both v3.0 models from their downloaded inputs.

    Returns at once when the lib's manifest says the files are current.
    """
    _downloader("acoustic").ensure_labels_available()
    _downloader("geo").ensure_labels_available()
