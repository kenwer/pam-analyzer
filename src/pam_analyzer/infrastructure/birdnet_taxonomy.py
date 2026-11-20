"""Version-neutral taxonomy services over the birdnet>=1.5 library.

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
one subclass, in birdnet_2_4_taxonomy and birdnet_3_0_taxonomy, and each of
those modules exposes its instance as TAXONOMY.

Each version pairs its acoustic model with the geo model of the same
generation, because the geo model supplies the allow-list that model output
is matched against and the two axes have to agree.

We deliberately do not import `birdnet` at module load, here or in the
version modules. Importing the lib triggers logging setup, and the runtime
import further down the call chain is not free. Every function imports
lazily so app startup does not pay that cost.

Label entries arrive as 'Scientific name_Common name' (one species per
line). The split happens here so callers see a plain {sci: common} dict.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from functools import cached_property
from typing import Any

from .species_names import canonical, canonical_set

# FP32 over FP16: onnxruntime's CPU provider upcasts FP16 per-op, so the
# smaller download would cost throughput on the hardware this app targets.
# Imported by the runners: the acoustic model each loads and the label maps
# read below have to resolve the same model files.
MODEL_PRECISION = "fp32"


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
