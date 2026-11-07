from pathlib import Path

from pam_analyzer.infrastructure.legacy_onefile_cache import legacy_cache_dir, remove_legacy_cache


def test_windows_cache_dir_is_under_local_appdata(tmp_path: Path) -> None:
    assert legacy_cache_dir("win32", {"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "de.ken" / "pam-analyzer"


def test_windows_without_local_appdata_has_no_cache_dir() -> None:
    assert legacy_cache_dir("win32", {}) is None


def test_linux_cache_dir_honors_xdg_cache_home(tmp_path: Path) -> None:
    assert legacy_cache_dir("linux", {"XDG_CACHE_HOME": str(tmp_path)}) == tmp_path / "de.ken" / "pam-analyzer"


def test_linux_cache_dir_defaults_to_home_cache() -> None:
    assert legacy_cache_dir("linux", {}) == Path.home() / ".cache" / "de.ken" / "pam-analyzer"


def test_macos_never_had_a_cache() -> None:
    assert legacy_cache_dir("darwin", {}) is None


def test_removes_every_version_and_the_empty_company_folder(tmp_path: Path) -> None:
    cache = tmp_path / "de.ken" / "pam-analyzer"
    for version in ("0.7.5", "0.8.0"):
        (cache / version / "birdnet-models").mkdir(parents=True)
        (cache / version / "birdnet-models" / "model.onnx").write_bytes(b"x")

    remove_legacy_cache(cache)

    assert not (tmp_path / "de.ken").exists()


def test_keeps_the_company_folder_when_another_product_lives_there(tmp_path: Path) -> None:
    cache = tmp_path / "de.ken" / "pam-analyzer"
    (cache / "0.8.0").mkdir(parents=True)
    (tmp_path / "de.ken" / "detectorist").mkdir()

    remove_legacy_cache(cache)

    assert not cache.exists()
    assert (tmp_path / "de.ken" / "detectorist").is_dir()


def test_missing_cache_is_a_no_op(tmp_path: Path) -> None:
    remove_legacy_cache(tmp_path / "absent")
    remove_legacy_cache(None)
