"""Tests for load_perch_v2_pinned resolving Perch v2 from the local cache.

The regression these tests guard: birdnet's own loader asks kagglehub for
an unversioned model handle, which forces a Kaggle API call before the
on-disk cache is consulted, so a warm (or bundled) cache still required
network access. The pinned loader requests a versioned handle, which
kagglehub resolves entirely from the cache.

These are fast tests: no network, no TensorFlow. AcousticModelPerchV2.load
only constructs the model wrapper, and the tests stub it out anyway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pam_analyzer.infrastructure.birdnet_lib import load_perch_v2_pinned
from pam_analyzer.infrastructure.model_versions import PERCH_V2_KAGGLE_VERSION
from pam_analyzer.infrastructure.perch_runner import PerchRunner

_LABELS_HEADER = "inat2024_fsd50k"
_N_SPECIES = 14795


def _build_fake_cache(cache_root: Path, n_species: int = _N_SPECIES) -> Path:
    """Create a kagglehub cache tree holding a completed pinned download.

    Layout mirrors kagglehub's version-keyed cache: the version directory
    plus the sibling <version>.complete marker that kagglehub writes after
    a successful download and checks before trusting the directory.
    """
    variation_dir = (
        cache_root
        / "models"
        / "google"
        / "bird-vocalization-classifier"
        / "tensorFlow2"
        / "perch_v2_cpu"
    )
    model_dir = variation_dir / str(PERCH_V2_KAGGLE_VERSION)
    assets = model_dir / "assets"
    assets.mkdir(parents=True)
    lines = [_LABELS_HEADER] + [f"sp{i}" for i in range(n_species)]
    (assets / "labels.csv").write_text("\n".join(lines) + "\n", encoding="utf8")
    (variation_dir / f"{PERCH_V2_KAGGLE_VERSION}.complete").touch()
    return model_dir


class _LoadCapture:
    """Stub for AcousticModelPerchV2.load that records its arguments."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        model_path: Path,
        species_list: Any,
        *,
        backend_type: type,
        backend_kwargs: dict,
    ) -> str:
        self.calls.append(
            {
                "model_path": model_path,
                "species_list": species_list,
                "backend_type": backend_type,
                "backend_kwargs": backend_kwargs,
            }
        )
        return "model-sentinel"


def test_resolves_from_cache_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm cache must satisfy the pinned load with zero network calls."""
    import kagglehub.clients
    import kagglehub.http_resolver
    from birdnet.acoustic.models.perch_v2.model import AcousticModelPerchV2
    from birdnet.acoustic.models.perch_v2.pb import AcousticPBBackendFP32PerchV2

    model_dir = _build_fake_cache(tmp_path)
    monkeypatch.setenv("KAGGLEHUB_CACHE", str(tmp_path))

    def _no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access attempted during cached load")

    monkeypatch.setattr(kagglehub.http_resolver, "_get_current_version", _no_network)
    monkeypatch.setattr(kagglehub.clients, "download_file", _no_network)

    capture = _LoadCapture()
    monkeypatch.setattr(AcousticModelPerchV2, "load", capture)

    result = load_perch_v2_pinned()

    assert result == "model-sentinel"
    (call,) = capture.calls
    assert call["model_path"] == model_dir
    assert len(call["species_list"]) == _N_SPECIES
    assert _LABELS_HEADER not in call["species_list"]
    assert call["backend_type"] is AcousticPBBackendFP32PerchV2
    assert call["backend_kwargs"] == {}


def test_requests_pinned_versioned_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kagglehub
    from birdnet.acoustic.models.perch_v2.model import AcousticModelPerchV2

    model_dir = _build_fake_cache(tmp_path)
    requested: list[str] = []

    def _fake_download(handle: str) -> str:
        requested.append(handle)
        return str(model_dir)

    monkeypatch.setattr(kagglehub, "model_download", _fake_download)
    monkeypatch.setattr(AcousticModelPerchV2, "load", _LoadCapture())

    load_perch_v2_pinned()

    expected = (
        "google/bird-vocalization-classifier/tensorFlow2/perch_v2_cpu"
        f"/{PERCH_V2_KAGGLE_VERSION}"
    )
    assert requested == [expected]


def test_load_model_uses_pinned_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PerchRunner must go through the pinned loader, not birdnet's."""
    import birdnet
    from birdnet.acoustic.models.perch_v2.model import AcousticModelPerchV2

    _build_fake_cache(tmp_path)
    monkeypatch.setenv("KAGGLEHUB_CACHE", str(tmp_path))

    def _unpinned_forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unpinned birdnet.load_perch_v2 must not be called")

    monkeypatch.setattr(birdnet, "load_perch_v2", _unpinned_forbidden)
    monkeypatch.setattr(AcousticModelPerchV2, "load", _LoadCapture())

    assert PerchRunner()._load_model() == "model-sentinel"


def test_rejects_wrong_species_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_fake_cache(tmp_path, n_species=10)
    monkeypatch.setenv("KAGGLEHUB_CACHE", str(tmp_path))

    with pytest.raises(ValueError, match="14795"):
        load_perch_v2_pinned()
