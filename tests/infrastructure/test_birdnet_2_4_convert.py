"""BirdNET v2.4's package downloads the Keras zip only until it is converted, then deletes it."""

from pathlib import Path

import pytest

from pam_analyzer.infrastructure import birdnet_2_4_convert, birdnet_2_4_keras_onnx
from pam_analyzer.infrastructure.birdnet_2_4_runner import Birdnet24Runner
from pam_analyzer.infrastructure.http_download import DownloadError
from pam_analyzer.infrastructure.model_store import install, marker_path
from tests.infrastructure.conftest import never, no_progress


@pytest.fixture
def app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BIRDNET_APP_DATA", str(tmp_path))
    return tmp_path


@pytest.fixture
def conversions(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace the real converter with one that writes placeholder outputs and records its zip."""
    calls: list[Path] = []

    def fake_convert(zip_path: Path, app_data: Path) -> None:
        calls.append(zip_path)
        for kind in ("acoustic", "geo"):
            labels = app_data / f"{kind}-models" / "v2.4" / "onnx" / "labels"
            labels.mkdir(parents=True, exist_ok=True)
            (labels.parent / "model-fp32.onnx").write_bytes(b"onnx")
            (labels / "en_us.txt").write_text("Parus major_Great Tit\n", encoding="utf-8")

    monkeypatch.setattr(birdnet_2_4_keras_onnx, "convert", fake_convert)
    return calls


def _place_verified_zip() -> Path:
    """Put the zip where a finished download leaves it, marker included, so install skips the download."""
    source = birdnet_2_4_convert.source_artifact()
    source.dest.parent.mkdir(parents=True, exist_ok=True)
    source.dest.write_bytes(b"zip")
    marker_path(source).write_text(source.sha256 + "\n", encoding="ascii")
    return source.dest


def test_package_is_wired_to_the_converter() -> None:
    assert Birdnet24Runner.package.artifacts is birdnet_2_4_convert.artifacts
    assert Birdnet24Runner.package.prepare is birdnet_2_4_convert.prepare


@pytest.mark.usefixtures("app_data")
def test_lists_the_zip_until_converted(conversions: list[Path]) -> None:
    assert [a.id for a in birdnet_2_4_convert.artifacts()] == ["birdnet-2.4-keras"]
    zip_path = _place_verified_zip()

    install(Birdnet24Runner.package, no_progress, never)

    assert conversions == [zip_path]
    assert birdnet_2_4_convert.artifacts() == ()
    assert not zip_path.exists()
    assert not marker_path(birdnet_2_4_convert.source_artifact()).exists()


@pytest.mark.usefixtures("app_data")
def test_prepare_does_nothing_once_converted(conversions: list[Path]) -> None:
    _place_verified_zip()
    birdnet_2_4_convert.prepare()
    birdnet_2_4_convert.prepare()
    assert len(conversions) == 1


@pytest.mark.usefixtures("app_data")
def test_failed_conversion_keeps_the_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_convert(_zip_path: Path, _app_data: Path) -> None:
        raise ValueError("unsupported layer")

    monkeypatch.setattr(birdnet_2_4_keras_onnx, "convert", broken_convert)
    zip_path = _place_verified_zip()

    with pytest.raises(DownloadError, match="unsupported layer"):
        install(Birdnet24Runner.package, no_progress, never)

    assert zip_path.exists()
    assert not birdnet_2_4_convert.converted()


@pytest.mark.usefixtures("conversions")
def test_a_new_conversion_revision_makes_outputs_stale(app_data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _place_verified_zip()
    birdnet_2_4_convert.prepare()
    monkeypatch.setattr(birdnet_2_4_convert, "_MARKER_TEXT", "other r2\n")
    assert not birdnet_2_4_convert.converted()


@pytest.mark.usefixtures("conversions")
def test_missing_labels_make_outputs_stale(app_data: Path) -> None:
    _place_verified_zip()
    birdnet_2_4_convert.prepare()
    for path in (app_data / "geo-models" / "v2.4" / "onnx" / "labels").iterdir():
        path.unlink()
    (app_data / "geo-models" / "v2.4" / "onnx" / "labels").rmdir()
    assert not birdnet_2_4_convert.converted()


@pytest.mark.usefixtures("conversions")
def test_outputs_without_a_marker_are_not_trusted(app_data: Path) -> None:
    """Files left by the removed conversion script carry no marker and get converted again."""
    _place_verified_zip()
    birdnet_2_4_convert.prepare()
    (app_data / "acoustic-models" / "v2.4" / "onnx" / "conversion.txt").unlink()
    assert not birdnet_2_4_convert.converted()
