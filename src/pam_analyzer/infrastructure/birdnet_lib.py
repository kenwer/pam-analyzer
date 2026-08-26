"""Adapter helpers wrapping the birdnet>=1.1 library.

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
its own model rather than reaching for a hardcoded version. TAXONOMY_V2_4 and
TAXONOMY_V3_0 are the two instances. The caches behind them are keyed by
version, so the two engines never evict each other's label maps.

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

from dataclasses import dataclass
from functools import cache, lru_cache
from typing import Any

from .species_names import canonical, canonical_set

# FP32 over FP16: onnxruntime's CPU provider upcasts FP16 per-op, so the
# smaller download would cost throughput on the hardware this app targets.
# Imported by the runners: the acoustic model each loads and the label maps
# read below have to resolve the same model files.
MODEL_PRECISION = "fp32"

ACOUSTIC_V2_4 = "2.4"
ACOUSTIC_V3_0 = "3.0"


def _label_base(version: str) -> Any:
    """Downloader base class naming the locales one version ships labels for.

    Only AVAILABLE_LANGUAGES is read off it, which downloads nothing. Where the
    label files themselves come from differs per version and is resolved in
    _locale_label_map instead.
    """
    if version == ACOUSTIC_V2_4:
        from birdnet.acoustic.models.v2_4.model import AcousticDownloaderBaseV2_4

        return AcousticDownloaderBaseV2_4

    from birdnet.acoustic.models.v3_0.model import AcousticDownloaderBaseV3_0

    return AcousticDownloaderBaseV3_0


@cache
def _geo_model_cached(version: str):  # noqa: ANN202
    """Load one version's geo model once per process.

    Loading triggers a one-time download into the lib's app-data directory
    (default ~/Library/Application Support/birdnet on macOS, overridable
    via BIRDNET_APP_DATA). Subsequent calls reuse the model.

    Language is fixed to en_us because we never surface the geo model's
    own common-name output. We only consume its scientific-name axis.

    Both engines run on ONNX, but only v3.0 can say so through birdnet.load:
    the lib ships no ONNX backend for either v2.4 model, so that call would
    raise. v2.4 goes through the locally-defined backend instead, against
    weights converted at build time.
    """
    if version == ACOUSTIC_V2_4:
        from . import birdnet_2_4_onnx

        return birdnet_2_4_onnx.load_geo("en_us")

    import birdnet

    from .birdnet_onnx_threads import pin_session_threads

    return pin_session_threads(
        birdnet.load("geo", ACOUSTIC_V3_0, "onnx", lang="en_us", precision=MODEL_PRECISION)
    )


def _split_sci_common(line: str) -> tuple[str, str]:
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


@cache
def _available_locales(version: str) -> tuple[str, ...]:
    return tuple(sorted(_label_base(version).AVAILABLE_LANGUAGES))


@cache
def _known_species(version: str) -> frozenset[str]:
    """One version's full species axis, built once per process.

    The label map below is already cached, but building a frozenset over its
    keys is not, and the analysis row loop asks per dropped detection.
    """
    return frozenset(_locale_label_map(version, "en_us"))


@lru_cache(maxsize=16)
def _locale_label_map(version: str, lang: str) -> dict[str, str]:
    lang = normalize_lang_code(lang)
    if lang not in _label_base(version).AVAILABLE_LANGUAGES:
        return {}

    if version == ACOUSTIC_V2_4:
        # Read from the converted model's own label directory. Going through
        # the lib's TFLite downloader instead would fetch 51 MB of weights the
        # app no longer runs, just to reach the text files beside them.
        from . import birdnet_2_4_onnx

        species = birdnet_2_4_onnx.labels("acoustic", lang)
    else:
        from birdnet.acoustic.models.v3_0.onnx import AcousticOnnxDownloaderV3_0

        # Triggers the model download on first call if absent. Subsequent
        # calls just read the label file.
        _, species = AcousticOnnxDownloaderV3_0.get_model_path_and_labels(lang, MODEL_PRECISION)

    mapping: dict[str, str] = {}
    for entry in species:
        sci, common = _split_sci_common(entry)
        if sci:
            mapping[sci] = common
    return _canonicalise_labels(mapping, version, lang)


def _canonicalise_labels(mapping: dict[str, str], version: str, lang: str) -> dict[str, str]:
    """Re-key a label map onto canonical species names.

    An entry already keyed on the canonical name wins, otherwise the
    superseded entry is promoted under it. That one rule serves both
    generations: v3.0 has a real Thinornis dubius entry to win with, and
    v2.4 has only Charadrius dubius, which is promoted carrying its
    translation.
    """
    out: dict[str, str] = {}
    for sci, common in mapping.items():
        key = canonical(sci)
        if key not in out or sci == key:
            out[key] = common

    if version == ACOUSTIC_V3_0 and lang == "de":
        # Upstream's taxonomy gives Tyto alba (Western Barn Owl) the German
        # name of Tyto furcata. Remove when upstream corrects common_name_de.
        out["Tyto alba"] = "Schleiereule"
    return out


@dataclass(frozen=True, slots=True)
class TaxonomyServices:
    """One model version's species axis, label maps and geo filter.

    A runner holds the instance matching the model it loads, so the shared
    pipeline in BaseAnalysisRunner can resolve species filters and common
    names without knowing which version is running.
    """

    version: str

    def region_species_scientific(self, lat: float, lon: float, week: int) -> frozenset[str]:
        """Canonical species names the geo model considers possible at
        (lat, lon, week).

        A `week` of -1 means 'no week filter' and is translated to the lib's
        `week=None`. Threshold 0.03 matches the lib's own default for the
        species-filter step.

        Canonicalised on the way out so the allow-list and the detection rows
        it is compared against speak one namespace. The geo model carries only
        current spellings, so an acoustic class under a superseded spelling
        would otherwise never match.
        """
        geo = _geo_model_cached(self.version)
        result = geo.predict(
            float(lat),
            float(lon),
            week=(None if week == -1 else week),
            min_confidence=0.03,
        )
        return canonical_set(_split_sci_common(name)[0] for name in result.to_set())

    def known_species_scientific(self) -> frozenset[str]:
        """Every scientific name on this model's label axis, ignoring region.

        Lets a caller tell two kinds of filter drop apart: a species the
        model knows but does not expect at a given location/week (a geography
        drop), versus a name the axis does not carry at all (a user typo, or
        a name from the other model's taxonomy).

        Sourced from the en_us label map, whose keys are the full species
        set. en_us is present in both versions' locale sets, so this never
        degrades to an empty set the way an arbitrary locale could.
        """
        return _known_species(self.version)

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
        return _locale_label_map(self.version, lang)

    def available_locales(self) -> tuple[str, ...]:
        """Locale codes this model version ships labels for.

        Reading AVAILABLE_LANGUAGES off the downloader answers the question
        without downloading anything.
        """
        return _available_locales(self.version)


TAXONOMY_V2_4 = TaxonomyServices(ACOUSTIC_V2_4)
TAXONOMY_V3_0 = TaxonomyServices(ACOUSTIC_V3_0)
