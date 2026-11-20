"""Adapter helpers wrapping the birdnet>=1.5 library.

An AnalysisRunner needs three pieces of taxonomy data from this library:

- A geographic species allowlist for a (lat, lon, week) triplet, to filter
  out predictions that are biologically implausible at the recording site.
- A {scientific_name: localized_common_name} mapping per requested locale,
  used to fill the per-locale common-name columns in the detections CSV.
- The list of locales the model has labels for, to populate the language
  picker in the UI.

All three are model-version specific: v2.4 and v3.0 label different class
sets, under different taxonomies, in different language sets. TaxonomyServices
binds one version's answers together so a runner can hold the services for
its own model rather than reaching for a hardcoded version. Each version is
one subclass, and TAXONOMY_V2_4 and TAXONOMY_V3_0 are their instances.

Each version pairs its acoustic model with the geo model of the same
generation, because the geo model supplies the allow-list that model output
is matched against and the two axes have to agree.

We deliberately do not import `birdnet` at module load. Importing the lib
triggers logging setup, and the runtime import further down the call chain
is not free. Every public function below imports lazily so app startup does
not pay that cost.

Label entries arrive as 'Scientific name_Common name' (one species per
line). The split happens here so callers see a plain {sci: common} dict.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from functools import cached_property
from typing import Any, Literal

from .model_store import Artifact
from .species_names import canonical, canonical_set

# FP32 over FP16: onnxruntime's CPU provider upcasts FP16 per-op, so the
# smaller download would cost throughput on the hardware this app targets.
# Imported by the runners: the acoustic model each loads and the label maps
# read below have to resolve the same model files.
MODEL_PRECISION = "fp32"

ACOUSTIC_V3_0 = "3.0"


def split_sci_common(line: str) -> tuple[str, str]:
    """Split a 'Scientific_Common' label entry into (sci, common).

    `partition` keeps any further underscores in the common name attached,
    which matches the upstream label format.
    """
    sci, _, common = line.partition("_")
    return sci, common


def normalize_lang_code(code: str) -> str:
    """Map legacy short codes ('en') to the lib's canonical codes ('en_us').

    Projects saved while the app was on birdnet_analyzer used short codes
    ('en', 'de', ...). The lib distinguishes 'en_us' from 'en_uk' and drops
    the bare 'en'. Treating stored 'en' as 'en_us' avoids breaking those
    projects without requiring a one-off migration of project TOMLs.
    """
    return "en_us" if code == "en" else code


def _v3_0_downloader(kind: Literal["acoustic", "geo"]):  # noqa: ANN202
    if kind == "acoustic":
        from birdnet.acoustic.models.v3_0.onnx import AcousticOnnxDownloaderV3_0

        return AcousticOnnxDownloaderV3_0
    from birdnet.geo.models.v3_0.onnx import GeoOnnxDownloaderV3_0

    return GeoOnnxDownloaderV3_0


def v3_0_labels(lang: str, kind: Literal["acoustic", "geo"] = "acoustic"):  # noqa: ANN201
    """One BirdNET v3.0 model's 'Scientific_Common' entries for one locale, in output order.

    Label files only: the lib generates them from its label and taxonomy
    downloads on first use, and the model weights are never touched.
    """
    import birdnet
    from birdnet.utils.species_lists import read_official_labels

    downloader = _v3_0_downloader(kind)
    model = birdnet.AcousticModelV3_0 if kind == "acoustic" else birdnet.GeoModelV3_0
    downloader.ensure_labels_available()
    return read_official_labels(downloader.get_lang_file(lang), model.get_n_classes(), f"{kind} model v3.0")


def _canonicalise_labels(mapping: dict[str, str]) -> dict[str, str]:
    """Re-key a label map onto canonical species names.

    An entry already keyed on the canonical name wins, otherwise the
    superseded entry is promoted under it. v2.4 has only Charadrius dubius,
    which is promoted carrying its translation. Where two classes of one
    model lump into a single species, the canonical one wins if present.
    """
    out: dict[str, str] = {}
    for sci, common in mapping.items():
        key = canonical(sci)
        if key not in out or sci == key:
            out[key] = common
    return out


# The taxon_group values BirdNET+ Taxonomy gives classes that are a sound
# rather than an animal (engine, siren, rain, speech). Read from the raw v3.0
# label file here and from the taxonomy CSV by scripts/build_species_aliases.py.
SOUND_CLASS_GROUPS = frozenset({"Anthropogenic", "Geophony", "Human", "Music", "Soundscape"})


class TaxonomyServices(ABC):
    """One model version's species axis, label maps and geo filter.

    A runner holds the instance matching the model it loads, so the shared
    pipeline in BaseAnalysisRunner can resolve species filters and common
    names without knowing which version is running. Each instance caches
    what it loads, so the two versions never evict each other's label maps.
    """

    def __init__(self) -> None:
        self._label_maps: dict[str, dict[str, str]] = {}

    @abstractmethod
    def available_locales(self) -> tuple[str, ...]:
        """Locale codes this model version ships labels for. Downloads nothing."""

    @abstractmethod
    def _label_entries(self, lang: str) -> Sequence[str]:
        """The acoustic model's 'Scientific_Common' entries for one available locale."""

    @abstractmethod
    def _load_geo_model(self) -> Any:
        """The geo model of the same generation, loaded with en_us.

        Language is fixed because only the geo model's scientific-name axis
        is consumed, never its common names.
        """

    def _sound_classes(self) -> frozenset[str]:
        """Canonical names of classes that are a sound rather than an animal."""
        return frozenset()

    def _label_map(self, lang: str) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for entry in self._label_entries(lang):
            sci, common = split_sci_common(entry)
            if sci:
                mapping[sci] = common
        return _canonicalise_labels(mapping)

    @cached_property
    def _geo_model(self) -> Any:
        return self._load_geo_model()

    @cached_property
    def _known_species(self) -> frozenset[str]:
        return frozenset(self.locale_label_map("en_us"))

    def region_species_scientific(self, lat: float, lon: float, week: int) -> frozenset[str]:
        """Canonical species names the geo model considers possible at
        (lat, lon, week).

        A `week` of -1 means 'no week filter' and is translated to the lib's
        `week=None`. Threshold 0.03 matches the lib's own default for the
        species-filter step.

        Canonicalised on the way out so the allow-list and the detection rows
        it is compared against speak one namespace. The geo model and the
        acoustic model each carry some superseded spellings, and they need not
        be the same ones, so only canonical names on both sides are certain to
        match.

        Sound classes are allowed everywhere. They do not depend on location,
        and the v3.0 geo model has no entry for them.
        """
        result = self._geo_model.predict(
            float(lat),
            float(lon),
            week=(None if week == -1 else week),
            min_confidence=0.03,
        )
        return canonical_set(split_sci_common(name)[0] for name in result.to_set()) | self._sound_classes()

    def known_species_scientific(self) -> frozenset[str]:
        """Every scientific name on this model's label axis, ignoring region.

        Lets a caller tell two kinds of filter drop apart: a species the
        model knows but does not expect at a given location/week (a geography
        drop), versus a name the axis does not carry at all (a user typo, or
        a name from the other model's taxonomy).

        Sourced from the en_us label map, whose keys are the full species
        set. en_us is present in both versions' locale sets, so this never
        degrades to an empty set the way an arbitrary locale could. Built
        once, because the analysis row loop asks per dropped detection.
        """
        return self._known_species

    def locale_label_map(self, lang: str) -> dict[str, str]:
        """{scientific_name: localized_common_name} for one language.

        Sourced from the acoustic model's own label files, so the keys are
        exactly the axis result rows carry. The geo model has a wider class
        set (it covers insects, amphibians and mammals the acoustic model
        does not emit), which makes it the wrong side to key common names on.

        Returns {} for a locale this version has no labels for rather than
        raising, so a project whose language only one engine ships degrades
        to no localization on the other rather than failing the run.
        """
        lang = normalize_lang_code(lang)
        if lang not in self.available_locales():
            return {}
        if lang not in self._label_maps:
            self._label_maps[lang] = self._label_map(lang)
        return self._label_maps[lang]


class _BirdnetV3_0(TaxonomyServices):
    def available_locales(self) -> tuple[str, ...]:
        import birdnet

        return tuple(sorted(birdnet.VALID_MODEL_LANGUAGES_V3_0))

    def _label_entries(self, lang: str) -> Sequence[str]:
        return v3_0_labels(lang)

    def _load_geo_model(self) -> Any:
        """Loading triggers a one-time download into the lib's app-data
        directory (default ~/Library/Application Support/birdnet on macOS,
        overridable via BIRDNET_APP_DATA).
        """
        import birdnet

        from .onnx_telemetry import disable_in_workers

        return disable_in_workers(
            birdnet.load("geo", ACOUSTIC_V3_0, "onnx", lang="en_us", precision=MODEL_PRECISION)
        )

    @cached_property
    def _sound_class_names(self) -> frozenset[str]:
        """Read from the raw label file, the only one that carries taxon_group."""
        import csv

        downloader = _v3_0_downloader("acoustic")
        downloader.ensure_labels_available()
        path = downloader._labels_input().path  # noqa: SLF001
        with open(path, encoding="utf-8-sig", newline="") as f:
            return canonical_set(
                row["scientific_name"].strip() for row in csv.DictReader(f) if row["taxon_group"] in SOUND_CLASS_GROUPS
            )

    def _sound_classes(self) -> frozenset[str]:
        return self._sound_class_names


class _BirdnetV2_4(TaxonomyServices):
    """BirdNET v2.4, whose common names come from a reference taxonomy.

    Its geo model lists its own sound classes, so they already reach the
    allow-list and _sound_classes stays empty.
    """

    def __init__(self, names_from: TaxonomyServices) -> None:
        super().__init__()
        self._names_from = names_from

    def available_locales(self) -> tuple[str, ...]:
        import birdnet

        return tuple(sorted(birdnet.VALID_MODEL_LANGUAGES_V2_4))

    def _label_entries(self, lang: str) -> Sequence[str]:
        """Read from the converted model's own label directory.

        Going through the lib's TFLite downloader instead would fetch 51 MB of
        weights the app no longer runs, just to reach the text files beside them.
        """
        from . import birdnet_2_4_onnx

        return birdnet_2_4_onnx.labels("acoustic", lang)

    def _load_geo_model(self) -> Any:
        """The lib ships no ONNX backend for either v2.4 model, so birdnet.load
        would raise. The locally-defined backend runs the weights converted on
        this machine instead.
        """
        from . import birdnet_2_4_onnx

        return birdnet_2_4_onnx.load_geo("en_us")

    def _label_map(self, lang: str) -> dict[str, str]:
        """Only values change: the keys stay v2.4's axis, so one species reads
        the same whichever engine found it without widening what v2.4 detects.
        """
        reference = self._names_from.locale_label_map(lang)
        return {name: reference.get(name) or common for name, common in super()._label_map(lang).items()}


TAXONOMY_V3_0: TaxonomyServices = _BirdnetV3_0()
TAXONOMY_V2_4: TaxonomyServices = _BirdnetV2_4(names_from=TAXONOMY_V3_0)


def _v3_0_artifact(kind: str) -> Artifact:
    """BirdNET v3.0's acoustic or geo weights, pinned by the lib, at the path the lib looks in."""
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
        dest=get_model_path(kind, ACOUSTIC_V3_0, "onnx", MODEL_PRECISION, content_tag=info.content_tag),
        stale_glob=f"model-{MODEL_PRECISION}*.onnx",
    )


def v3_0_geo_artifact() -> Artifact:
    """BirdNET v3.0's geo model, also the location filter of Perch runs."""
    return _v3_0_artifact("geo")


def v3_0_label_artifacts() -> tuple[Artifact, ...]:
    """The raw label files and the taxonomy the v3.0 locale files are generated from.

    Downloaded to the paths the lib reads, so prepare_v3_0_labels() finds
    them verified in place and fetches nothing itself. _labels_input() is
    private birdnet API, pinned by test_v3_0_label_inputs_download_to_the_lib_paths.
    """
    from birdnet.utils.taxonomy_v3 import get_taxonomy_v3_input

    inputs = {
        "birdnet-3.0-acoustic-labels": _v3_0_downloader("acoustic")._labels_input(),  # noqa: SLF001
        "birdnet-3.0-geo-labels": _v3_0_downloader("geo")._labels_input(),  # noqa: SLF001
        "birdnet-3.0-taxonomy": get_taxonomy_v3_input(),
    }
    return tuple(
        Artifact(id=key, url=i.url, size=i.size, sha256=i.sha256, dest=i.path) for key, i in inputs.items()
    )


def v3_0_artifacts() -> tuple[Artifact, ...]:
    """Everything a BirdNET v3.0 run reads: both models and their label inputs."""
    return (_v3_0_artifact("acoustic"), v3_0_geo_artifact(), *v3_0_label_artifacts())


def prepare_v3_0_labels() -> None:
    """Generate the per-locale label files of both v3.0 models from their downloaded inputs.

    Returns at once when the lib's manifest says the files are current.
    """
    _v3_0_downloader("acoustic").ensure_labels_available()
    _v3_0_downloader("geo").ensure_labels_available()
