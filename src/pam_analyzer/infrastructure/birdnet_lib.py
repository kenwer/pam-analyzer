"""Adapter helpers wrapping the birdnet>=0.2 library.

Both BirdnetRunner and PerchRunner need three pieces from this library:

- A geographic species whitelist for a (lat, lon, week) triplet, to filter
  out predictions that are biologically implausible at the recording site.
  Replaces birdnet_analyzer.species.utils.get_species_list.
- A {scientific_name: localized_common_name} mapping per requested locale,
  sourced from the label files the lib downloads alongside the geo model.
  Replaces birdnet_runner._load_locale_labels.
- The list of locales the v2.4 model has labels for, to populate the
  language picker in the UI. Replaces birdnet_runner._get_available_locales.

We deliberately do not import `birdnet` at module load. Loading the lib
triggers logging setup and bringing in `birdnet` is cheap, but `import
tensorflow` further down the call chain is not. Every public function
below imports lazily so app startup does not pay that cost.

Label files live under {BIRDNET_APP_DATA}/geo-models/v2.4/tf/labels/{lang}.txt
in 'Scientific name_Common name' format (one species per line). The split
happens here so callers see a plain {sci: common} dict.
"""

from __future__ import annotations

from functools import cache, lru_cache


def _split_sci_common(line: str) -> tuple[str, str]:
    """Split a 'Scientific_Common' label entry into (sci, common).

    `partition` keeps any further underscores in the common name attached,
    which matches the upstream label format (e.g. names like
    'Pernis_apivorus_European Honey-buzzard' do not occur in practice but
    the parser is robust against them).
    """
    sci, _, common = line.partition("_")
    return sci, common


@cache
def _geo_model_cached():  # noqa: ANN202
    """Load the geo model once per process.

    Loading triggers a one-time ~45 MB download into the lib's app-data
    directory (default ~/Library/Application Support/birdnet on macOS,
    overridable via BIRDNET_APP_DATA). Subsequent calls reuse the model.

    Language is fixed to en_us because we never surface the geo model's
    own common-name output; we only consume its scientific-name axis.
    """
    import birdnet

    return birdnet.load("geo", "2.4", "tf", lang="en_us")


def region_species_scientific(lat: float, lon: float, week: int) -> frozenset[str]:
    """Scientific names BirdNET considers possible at (lat, lon, week).

    Threshold 0.03 matches what birdnet_analyzer.species.utils.get_species_list
    used internally for its species-filter step. A `week` of -1 means
    'no week filter' and is translated to the lib's `week=None`.
    """
    geo = _geo_model_cached()
    result = geo.predict(
        float(lat),
        float(lon),
        week=(None if week == -1 else week),
        min_confidence=0.03,
    )
    return frozenset(_split_sci_common(name)[0] for name in result.to_set())


def normalize_lang_code(code: str) -> str:
    """Map legacy short codes ('en') to the new lib's canonical codes ('en_us').

    Projects saved while the app was on birdnet_analyzer used short codes
    ('en', 'de', ...). The new lib distinguishes 'en_us' from 'en_uk', and
    drops the bare 'en'. Treating stored 'en' as 'en_us' avoids breaking
    those projects without requiring a one-off migration of project TOMLs.
    """
    return "en_us" if code == "en" else code


@cache
def available_locales() -> tuple[str, ...]:
    """Locale codes the v2.4 model ships labels for.

    Returned as a sorted tuple of canonical codes (e.g. 'de', 'en_us', 'fr').
    Used by the UI's language picker. The geo and acoustic models share
    the same locale set in this version of the lib, so either downloader's
    AVAILABLE_LANGUAGES is authoritative.
    """
    from birdnet.geo.models.v2_4.model import GeoDownloaderBaseV2_4

    return tuple(sorted(GeoDownloaderBaseV2_4.AVAILABLE_LANGUAGES))


@lru_cache(maxsize=8)
def locale_label_map(lang: str) -> dict[str, str]:
    """{scientific_name: localized_common_name} for one language.

    Sources from the geo model's label files rather than the acoustic
    model's because the geo model is already downloaded for the region
    whitelist; we avoid forcing a 76 MB acoustic download just to label
    output rows in Perch-only workflows. The two label sets cover the
    same V2.4 species.

    Returns {} for unknown locales rather than raising, so a stale 'en'
    code in a project file degrades to no localization rather than an
    exception.
    """
    lang = normalize_lang_code(lang)
    from birdnet.geo.models.v2_4.model import GeoDownloaderBaseV2_4
    from birdnet.geo.models.v2_4.tf import GeoTFDownloaderV2_4

    if lang not in GeoDownloaderBaseV2_4.AVAILABLE_LANGUAGES:
        return {}
    # Triggers the geo model download on first call if absent. Subsequent
    # calls just read the label file.
    _, species = GeoTFDownloaderV2_4.get_model_path_and_labels(lang)
    mapping: dict[str, str] = {}
    for entry in species:
        sci, common = _split_sci_common(entry)
        if sci:
            mapping[sci] = common
    return mapping
