"""Adapter helpers wrapping the birdnet>=1.1 library.

BirdnetRunner needs three pieces from this library:

- A geographic species whitelist for a (lat, lon, week) triplet, to filter
  out predictions that are biologically implausible at the recording site.
- A {scientific_name: localized_common_name} mapping per requested locale,
  used to fill the per-locale common-name columns in the detections CSV.
- The list of locales the model has labels for, to populate the language
  picker in the UI.

We deliberately do not import `birdnet` at module load. Importing the lib
triggers logging setup, and the ONNX runtime import further down the call
chain is not free. Every public function below imports lazily so app
startup does not pay that cost.

Label entries arrive as 'Scientific name_Common name' (one species per
line). The split happens here so callers see a plain {sci: common} dict.
"""

from __future__ import annotations

from functools import cache, lru_cache

# FP32 over FP16: onnxruntime's CPU provider upcasts FP16 per-op, so the
# smaller download would cost throughput on the hardware this app targets.
# Defined here and imported by BirdnetRunner: the acoustic model it loads and
# the label maps read below have to resolve the same model files.
MODEL_PRECISION = "fp32"


@cache
def _geo_model_cached():  # noqa: ANN202
    """Load the geo model once per process.

    Loading triggers a one-time download into the lib's app-data directory
    (default ~/Library/Application Support/birdnet on macOS, overridable
    via BIRDNET_APP_DATA). Subsequent calls reuse the model.

    Language is fixed to en_us because we never surface the geo model's
    own common-name output; we only consume its scientific-name axis.
    """
    import birdnet

    return birdnet.load("geo", "3.0", "onnx", lang="en_us", precision=MODEL_PRECISION)


def region_species_scientific(lat: float, lon: float, week: int) -> frozenset[str]:
    """Scientific names BirdNET considers possible at (lat, lon, week).

    A `week` of -1 means 'no week filter' and is translated to the lib's
    `week=None`. Threshold 0.03 matches the lib's own default for the
    species-filter step.
    """
    geo = _geo_model_cached()
    result = geo.predict(
        float(lat),
        float(lon),
        week=(None if week == -1 else week),
        min_confidence=0.03,
    )
    return frozenset(_split_sci_common(name)[0] for name in result.to_set())


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
def available_locales() -> tuple[str, ...]:
    """Locale codes the v3.0 model ships labels for.

    Returned as a sorted tuple of canonical codes (e.g. 'de', 'en_us',
    'fr'). Used by the UI's language picker. Reading AVAILABLE_LANGUAGES
    off the downloader avoids downloading anything to answer the question.

    v3.0 dropped Estonian ('et') when it moved to the shared taxonomy, so
    a project that stored 'et' now falls through locale_label_map's
    unknown-locale path and simply gets no localization.
    """
    from birdnet.acoustic.models.v3_0.model import AcousticDownloaderBaseV3_0

    return tuple(sorted(AcousticDownloaderBaseV3_0.AVAILABLE_LANGUAGES))


@cache
def known_species_scientific() -> frozenset[str]:
    """Every scientific name on the model's label axis, ignoring region.

    Lets a caller tell two kinds of filter drop apart: a species the model
    knows but does not expect at a given location/week (a geography drop),
    versus a name the axis does not carry at all (a user typo, or a name
    from an older taxonomy).

    Sourced from the en_us label map, whose keys are the full species set.
    en_us is always present in the shipped locale set, so this never
    degrades to an empty set the way an arbitrary locale could.
    """
    return frozenset(locale_label_map("en_us"))


@lru_cache(maxsize=8)
def locale_label_map(lang: str) -> dict[str, str]:
    """{scientific_name: localized_common_name} for one language.

    Sourced from the acoustic model's own label files, so the keys are
    exactly the axis result rows carry. The geo model has a wider class
    set (it covers insects, amphibians and mammals the acoustic model does
    not emit), which makes it the wrong side to key common names on.

    Returns {} for unknown locales rather than raising, so a stale code in
    a project file degrades to no localization rather than an exception.
    """
    lang = normalize_lang_code(lang)
    from birdnet.acoustic.models.v3_0.model import AcousticDownloaderBaseV3_0
    from birdnet.acoustic.models.v3_0.onnx import AcousticOnnxDownloaderV3_0

    if lang not in AcousticDownloaderBaseV3_0.AVAILABLE_LANGUAGES:
        return {}
    # Triggers the model download on first call if absent. Subsequent calls
    # just read the label file.
    _, species = AcousticOnnxDownloaderV3_0.get_model_path_and_labels(lang, MODEL_PRECISION)
    mapping: dict[str, str] = {}
    for entry in species:
        sci, common = _split_sci_common(entry)
        if sci:
            mapping[sci] = common
    return mapping
