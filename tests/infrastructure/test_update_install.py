import shutil
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from pam_analyzer.infrastructure import update_install
from pam_analyzer.infrastructure.update_feed import Update, UpdateError
from pam_analyzer.infrastructure.update_install import (
    DownloadCancelled,
    InstallTarget,
    apply_update,
    cleanup_after_update,
    detect_install_target,
    download,
    fetch_payload,
    free_space_ok,
    prepare,
    ready_payload,
)
from tests.infrastructure.conftest import Route


def make_update(version: str = "0.9.0", size: int = 1000, sha256: str | None = "0" * 64, asset: str = "a.bin") -> Update:
    return Update(version, "", "", asset, "http://example.invalid/a.bin", size, sha256)


def test_windows_target_is_the_executable_folder(tmp_path: Path) -> None:
    exe = tmp_path / "PAM Analyzer" / "PAM-Analyzer.exe"
    exe.parent.mkdir()
    (exe.parent / "unins000.exe").write_bytes(b"MZ")
    target = detect_install_target("win32", exe, {"TEMP": str(tmp_path / "t")})
    assert target == InstallTarget("windows", tmp_path / "PAM Analyzer", tmp_path / "t" / "pam-analyzer-update")


def test_macos_target_is_the_enclosing_app(tmp_path: Path) -> None:
    exe = tmp_path / "Apps" / "PAM-Analyzer.app" / "Contents" / "MacOS" / "PAM-Analyzer"
    target = detect_install_target("darwin", exe, {})
    assert target == InstallTarget(
        "macos", tmp_path / "Apps" / "PAM-Analyzer.app", tmp_path / "Apps" / ".pam-analyzer-update"
    )


def test_macos_without_an_app_bundle_has_no_target(tmp_path: Path) -> None:
    assert detect_install_target("darwin", tmp_path / "bin" / "python", {}) is None


def test_linux_target_is_the_appimage(tmp_path: Path) -> None:
    appimage = tmp_path / "PAM-Analyzer.AppImage"
    target = detect_install_target("linux", Path("/tmp/.mount_x/PAM-Analyzer"), {"APPIMAGE": str(appimage)})
    assert target == InstallTarget("linux", appimage, tmp_path / ".pam-analyzer-update")


def test_linux_without_appimage_has_no_target(tmp_path: Path) -> None:
    assert detect_install_target("linux", tmp_path / "PAM-Analyzer", {}) is None


def test_writable_folder_is_writable(tmp_path: Path) -> None:
    app = tmp_path / "PAM-Analyzer.app"
    app.mkdir()
    assert InstallTarget("macos", app, tmp_path / ".pam-analyzer-update").writable()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_read_only_folder_is_not_writable(tmp_path: Path) -> None:
    folder = tmp_path / "ro"
    (folder / "PAM-Analyzer.app").mkdir(parents=True)
    folder.chmod(0o500)
    try:
        assert not InstallTarget("macos", folder / "PAM-Analyzer.app", folder / ".pam-analyzer-update").writable()
    finally:
        folder.chmod(0o700)


def test_version_dir_is_per_version(tmp_path: Path) -> None:
    target = InstallTarget("linux", tmp_path / "a.AppImage", tmp_path / "stage")
    assert target.version_dir(make_update("0.9.0")) == tmp_path / "stage" / "0.9.0"


def test_free_space_needs_two_and_a_half_times_the_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = InstallTarget("linux", tmp_path / "a.AppImage", tmp_path / "stage")
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(update_install.shutil, "disk_usage", lambda _p: usage(0, 0, 2500))
    assert free_space_ok(target, make_update(size=1000))
    monkeypatch.setattr(update_install.shutil, "disk_usage", lambda _p: usage(0, 0, 2499))
    assert not free_space_ok(target, make_update(size=1000))


def test_cleanup_removes_applied_and_older_staged_versions_and_old_app(tmp_path: Path) -> None:
    app = tmp_path / "PAM-Analyzer.app"
    app.mkdir()
    (tmp_path / "PAM-Analyzer.app.old" / "Contents").mkdir(parents=True)
    stage = tmp_path / ".pam-analyzer-update"
    for version in ("0.8.0", "0.9.0", "0.10.0", "not-a-version"):
        (stage / version).mkdir(parents=True)
    cleanup_after_update(InstallTarget("macos", app, stage), "0.9.0")
    assert sorted(p.name for p in stage.iterdir()) == ["0.10.0", "not-a-version"]
    assert not (tmp_path / "PAM-Analyzer.app.old").exists()
    assert app.is_dir()


def test_cleanup_removes_an_old_appimage(tmp_path: Path) -> None:
    appimage = tmp_path / "PAM-Analyzer.AppImage"
    appimage.write_bytes(b"new")
    (tmp_path / "PAM-Analyzer.AppImage.old").write_bytes(b"old")
    cleanup_after_update(InstallTarget("linux", appimage, tmp_path / ".pam-analyzer-update"), "0.9.0")
    assert not (tmp_path / "PAM-Analyzer.AppImage.old").exists()


def test_cleanup_without_target_or_staging_is_a_no_op(tmp_path: Path) -> None:
    cleanup_after_update(None, "0.9.0")
    cleanup_after_update(InstallTarget("windows", tmp_path / "app", tmp_path / "missing"), "0.9.0")



BLOB = bytes(range(256)) * 20_000  # 5 MB, several download chunks


def served_update(server, version: str = "0.9.0", sha256: str | None = None) -> Update:
    import hashlib

    server.routes["/a.bin"] = Route(BLOB)
    digest = hashlib.sha256(BLOB).hexdigest() if sha256 is None else sha256
    return Update(version, "", "", "a.bin", server.url("/a.bin"), len(BLOB), digest)


def no_progress(_done: int, _total: int) -> None:
    pass


def never() -> bool:
    return False


def test_download_writes_verified_file(tmp_path: Path, local_http_server) -> None:
    dest = tmp_path / "0.9.0" / "a.bin"
    download(served_update(local_http_server), dest, "PAM-Analyzer/0.8.0", no_progress, never)
    assert dest.read_bytes() == BLOB
    assert not (tmp_path / "0.9.0" / "a.bin.part").exists()


def test_progress_reports_done_and_total(tmp_path: Path, local_http_server) -> None:
    calls = []
    download(served_update(local_http_server), tmp_path / "a.bin", "UA", lambda d, t: calls.append((d, t)), never)
    assert len(calls) > 1
    assert calls[-1] == (len(BLOB), len(BLOB))


def test_interrupted_download_resumes_with_range(tmp_path: Path, local_http_server) -> None:
    update = served_update(local_http_server)
    dest = tmp_path / "a.bin"
    local_http_server.cut_after = 1_500_000
    with pytest.raises(UpdateError):
        download(update, dest, "UA", no_progress, never)
    part = tmp_path / "a.bin.part"
    assert part.stat().st_size == 1_500_000
    download(update, dest, "UA", no_progress, never)
    assert local_http_server.requests[-1]["Range"] == "bytes=1500000-"
    assert dest.read_bytes() == BLOB


def test_server_ignoring_range_restarts_from_zero(tmp_path: Path, local_http_server) -> None:
    update = served_update(local_http_server)
    (tmp_path / "a.bin.part").write_bytes(b"x" * 1000)
    local_http_server.honor_range = False
    download(update, tmp_path / "a.bin", "UA", no_progress, never)
    assert (tmp_path / "a.bin").read_bytes() == BLOB


def test_digest_mismatch_deletes_part_and_raises(tmp_path: Path, local_http_server) -> None:
    update = served_update(local_http_server, sha256="0" * 64)
    with pytest.raises(UpdateError):
        download(update, tmp_path / "a.bin", "UA", no_progress, never)
    assert not (tmp_path / "a.bin.part").exists()
    assert not (tmp_path / "a.bin").exists()


def test_missing_digest_refuses_to_download(tmp_path: Path, local_http_server) -> None:
    update = served_update(local_http_server)
    update = Update(update.version, "", "", update.asset_name, update.asset_url, update.size, None)
    with pytest.raises(UpdateError):
        download(update, tmp_path / "a.bin", "UA", no_progress, never)
    assert local_http_server.requests == []


def test_cancel_keeps_part_and_raises(tmp_path: Path, local_http_server) -> None:
    with pytest.raises(DownloadCancelled):
        download(served_update(local_http_server), tmp_path / "a.bin", "UA", no_progress, lambda: True)
    assert (tmp_path / "a.bin.part").exists()


def test_prepare_linux_makes_the_appimage_executable(tmp_path: Path) -> None:
    archive = tmp_path / "0.9.0" / "PAM-Analyzer-linux-x86_64.AppImage"
    archive.parent.mkdir()
    archive.write_bytes(b"elf")
    target = InstallTarget("linux", tmp_path / "PAM-Analyzer.AppImage", tmp_path)
    assert prepare(target, make_update(), archive) == archive
    if sys.platform != "win32":
        assert archive.stat().st_mode & 0o111


def test_prepare_windows_returns_the_installer(tmp_path: Path) -> None:
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"MZ")
    assert prepare(InstallTarget("windows", tmp_path / "app", tmp_path), make_update(), setup) == setup


def _fake_dmg(tmp_path: Path, version: str) -> Path:
    import plistlib
    import subprocess

    src = tmp_path / "src"
    contents = src / "PAM-Analyzer.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "MacOS" / "PAM-Analyzer").write_text("#!/bin/sh\n")
    (contents / "Resources").mkdir()
    (contents / "MacOS" / "models").symlink_to("../Resources")
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleShortVersionString": version}, f)
    dmg = tmp_path / "x.dmg"
    subprocess.run(["hdiutil", "create", "-srcfolder", str(src), "-ov", "-quiet", str(dmg)], check=True)
    return dmg


@pytest.mark.skipif(sys.platform != "darwin", reason="hdiutil")
def test_prepare_macos_extracts_and_checks_the_bundle(tmp_path: Path) -> None:
    target = InstallTarget("macos", tmp_path / "Apps" / "PAM-Analyzer.app", tmp_path / "stage")
    update = make_update("0.9.0")
    archive = target.version_dir(update) / "x.dmg"
    archive.parent.mkdir(parents=True)
    shutil.move(_fake_dmg(tmp_path, "0.9.0"), archive)
    app = prepare(target, update, archive)
    assert app == target.version_dir(update) / "PAM-Analyzer.app"
    assert (app / "Contents" / "MacOS" / "models").is_symlink()


@pytest.mark.skipif(sys.platform != "darwin", reason="hdiutil")
def test_prepare_macos_rejects_a_bundle_of_another_version(tmp_path: Path) -> None:
    target = InstallTarget("macos", tmp_path / "Apps" / "PAM-Analyzer.app", tmp_path / "stage")
    update = make_update("0.9.0")
    archive = target.version_dir(update) / "x.dmg"
    archive.parent.mkdir(parents=True)
    shutil.move(_fake_dmg(tmp_path, "0.8.0"), archive)
    with pytest.raises(UpdateError):
        prepare(target, update, archive)


def test_ready_payload_rechecks_the_digest(tmp_path: Path) -> None:
    import hashlib

    target = InstallTarget("linux", tmp_path / "PAM-Analyzer.AppImage", tmp_path / "stage")
    update = make_update(sha256=hashlib.sha256(b"payload").hexdigest(), asset="new.AppImage")
    dest = target.version_dir(update) / "new.AppImage"
    assert ready_payload(target, update) is None
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"payload")
    assert ready_payload(target, update) == dest
    dest.write_bytes(b"tampered")
    assert ready_payload(target, update) is None


class Recorder:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> None:
        self.commands.append(command)


def _bundle(path: Path, marker: str) -> Path:
    path.mkdir(parents=True)
    (path / "marker").write_text(marker)
    return path


def test_windows_starts_the_installer_silently_with_relaunch(tmp_path: Path) -> None:
    launch = Recorder()
    setup = tmp_path / "setup.exe"
    apply_update(InstallTarget("windows", tmp_path / "PAM Analyzer", tmp_path), setup, launch)
    assert launch.commands == [[str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/RELAUNCH"]]


def test_macos_swaps_the_bundle_and_opens_the_new_one(tmp_path: Path) -> None:
    apps = tmp_path / "My Apps"
    app = _bundle(apps / "PAM-Analyzer.app", "old")
    payload = _bundle(apps / ".pam-analyzer-update" / "0.9.0" / "PAM-Analyzer.app", "new")
    launch = Recorder()
    apply_update(InstallTarget("macos", app, apps / ".pam-analyzer-update"), payload, launch)
    assert (app / "marker").read_text() == "new"
    assert (apps / "PAM-Analyzer.app.old" / "marker").read_text() == "old"
    assert launch.commands == [["open", "-n", str(app)]]


def test_linux_swaps_the_appimage_and_starts_it(tmp_path: Path) -> None:
    appimage = tmp_path / "PAM-Analyzer.AppImage"
    appimage.write_text("old")
    payload = tmp_path / ".pam-analyzer-update" / "0.9.0" / "PAM-Analyzer-linux-x86_64.AppImage"
    payload.parent.mkdir(parents=True)
    payload.write_text("new")
    launch = Recorder()
    apply_update(InstallTarget("linux", appimage, tmp_path / ".pam-analyzer-update"), payload, launch)
    assert appimage.read_text() == "new"
    assert (tmp_path / "PAM-Analyzer.AppImage.old").read_text() == "old"
    assert launch.commands == [[str(appimage)]]


def test_a_leftover_old_copy_is_replaced(tmp_path: Path) -> None:
    app = _bundle(tmp_path / "PAM-Analyzer.app", "current")
    _bundle(tmp_path / "PAM-Analyzer.app.old", "older")
    payload = _bundle(tmp_path / "stage" / "0.9.0" / "PAM-Analyzer.app", "new")
    apply_update(InstallTarget("macos", app, tmp_path / "stage"), payload, Recorder())
    assert (tmp_path / "PAM-Analyzer.app.old" / "marker").read_text() == "current"
    assert (app / "marker").read_text() == "new"


def test_failed_swap_restores_and_restarts_the_old_app(tmp_path: Path) -> None:
    app = _bundle(tmp_path / "PAM-Analyzer.app", "old")
    launch = Recorder()
    apply_update(InstallTarget("macos", app, tmp_path / "stage"), tmp_path / "stage" / "missing.app", launch)
    assert (app / "marker").read_text() == "old"
    assert not (tmp_path / "PAM-Analyzer.app.old").exists()
    assert launch.commands == [["open", "-n", str(app)]]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_failed_first_rename_restarts_the_old_app(tmp_path: Path) -> None:
    folder = tmp_path / "ro"
    appimage = folder / "PAM-Analyzer.AppImage"
    folder.mkdir()
    appimage.write_text("old")
    payload = tmp_path / "new.AppImage"
    payload.write_text("new")
    folder.chmod(0o500)
    launch = Recorder()
    try:
        apply_update(InstallTarget("linux", appimage, tmp_path / "stage"), payload, launch)
    finally:
        folder.chmod(0o700)
    assert appimage.read_text() == "old"
    assert launch.commands == [[str(appimage)]]


def _staged_macos(tmp_path: Path, plist_version: str | None) -> tuple[InstallTarget, Update]:
    import hashlib
    import plistlib

    target = InstallTarget("macos", tmp_path / "PAM-Analyzer.app", tmp_path / "stage")
    update = make_update("0.9.0", sha256=hashlib.sha256(b"dmg").hexdigest(), asset="x.dmg")
    vdir = target.version_dir(update)
    vdir.mkdir(parents=True)
    (vdir / "x.dmg").write_bytes(b"dmg")
    contents = vdir / "PAM-Analyzer.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "MacOS" / "PAM-Analyzer").write_text("bin")
    if plist_version is not None:
        with (contents / "Info.plist").open("wb") as f:
            plistlib.dump({"CFBundleShortVersionString": plist_version}, f)
    return target, update


def test_ready_payload_rejects_a_staged_bundle_of_another_version(tmp_path: Path) -> None:
    target, update = _staged_macos(tmp_path, "0.8.0")
    assert ready_payload(target, update) is None


def test_ready_payload_rejects_a_half_extracted_bundle(tmp_path: Path) -> None:
    target, update = _staged_macos(tmp_path, None)
    assert ready_payload(target, update) is None


def test_ready_payload_accepts_a_checked_bundle(tmp_path: Path) -> None:
    target, update = _staged_macos(tmp_path, "0.9.0")
    assert ready_payload(target, update) == target.version_dir(update) / "PAM-Analyzer.app"


@pytest.mark.skipif(sys.platform != "darwin", reason="hdiutil")
def test_rejected_bundle_is_not_left_where_it_could_be_installed(tmp_path: Path) -> None:
    target = InstallTarget("macos", tmp_path / "Apps" / "PAM-Analyzer.app", tmp_path / "stage")
    update = make_update("0.9.0")
    archive = target.version_dir(update) / "x.dmg"
    archive.parent.mkdir(parents=True)
    shutil.move(_fake_dmg(tmp_path, "0.8.0"), archive)
    with pytest.raises(UpdateError):
        prepare(target, update, archive)
    assert sorted(p.name for p in target.version_dir(update).iterdir()) == ["x.dmg"]


def test_relaunch_drops_a_model_path_inside_the_old_bundle() -> None:
    from pam_analyzer.infrastructure.update_install import relaunch_env

    env = relaunch_env({"BIRDNET_APP_DATA": "/tmp/.mount_old/birdnet-models/x", "HOME": "/h"}, Path("/tmp/.mount_old"))
    assert env == {"HOME": "/h"}


def test_relaunch_keeps_a_model_path_the_user_set() -> None:
    from pam_analyzer.infrastructure.update_install import relaunch_env

    env = relaunch_env({"BIRDNET_APP_DATA": "/data/models"}, Path("/tmp/.mount_old"))
    assert env == {"BIRDNET_APP_DATA": "/data/models"}


def test_a_complete_part_file_is_verified_without_a_request(tmp_path: Path, local_http_server) -> None:
    update = served_update(local_http_server)
    (tmp_path / "a.bin.part").write_bytes(BLOB)
    download(update, tmp_path / "a.bin", "UA", no_progress, never)
    assert (tmp_path / "a.bin").read_bytes() == BLOB
    assert local_http_server.requests == []


def test_a_complete_but_wrong_part_file_starts_over(tmp_path: Path, local_http_server) -> None:
    update = served_update(local_http_server)
    (tmp_path / "a.bin.part").write_bytes(b"x" * len(BLOB))
    with pytest.raises(UpdateError):
        download(update, tmp_path / "a.bin", "UA", no_progress, never)
    assert not (tmp_path / "a.bin.part").exists()
    download(update, tmp_path / "a.bin", "UA", no_progress, never)
    assert (tmp_path / "a.bin").read_bytes() == BLOB


def test_a_failed_swap_leaves_a_note_for_the_next_session(tmp_path: Path) -> None:
    from pam_analyzer.infrastructure.update_install import take_apply_failure

    app = _bundle(tmp_path / "PAM-Analyzer.app", "old")
    target = InstallTarget("macos", app, tmp_path / "stage")
    apply_update(target, tmp_path / "stage" / "missing.app", Recorder())
    note = take_apply_failure(target)
    assert note
    assert take_apply_failure(target) is None


def test_a_successful_swap_leaves_no_note(tmp_path: Path) -> None:
    from pam_analyzer.infrastructure.update_install import take_apply_failure

    app = _bundle(tmp_path / "PAM-Analyzer.app", "old")
    payload = _bundle(tmp_path / "stage" / "0.9.0" / "PAM-Analyzer.app", "new")
    target = InstallTarget("macos", app, tmp_path / "stage")
    apply_update(target, payload, Recorder())
    assert take_apply_failure(target) is None


def test_verified_archive_checks_the_digest(tmp_path: Path) -> None:
    import hashlib

    from pam_analyzer.infrastructure.update_install import verified_archive

    target = InstallTarget("macos", tmp_path / "PAM-Analyzer.app", tmp_path / "stage")
    update = make_update(sha256=hashlib.sha256(b"dmg").hexdigest(), asset="x.dmg")
    assert verified_archive(target, update) is None
    archive = target.version_dir(update) / "x.dmg"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"dmg")
    assert verified_archive(target, update) == archive
    archive.write_bytes(b"bad")
    assert verified_archive(target, update) is None


def test_a_failed_launch_is_logged_not_raised(tmp_path: Path) -> None:
    """setup.exe removed by a TEMP cleaner must not crash the exit."""
    from pam_analyzer.infrastructure.update_install import take_apply_failure

    def missing(command: list[str]) -> None:
        raise FileNotFoundError(command[0])

    target = InstallTarget("windows", tmp_path / "PAM Analyzer", tmp_path / "t")
    apply_update(target, tmp_path / "t" / "0.9.0" / "setup.exe", missing)
    assert take_apply_failure(target)


def test_windows_target_needs_the_installers_uninstaller(tmp_path: Path) -> None:
    """An exe outside the installed folder cannot be updated by Setup."""
    exe = tmp_path / "copy" / "PAM-Analyzer.exe"
    exe.parent.mkdir()
    assert detect_install_target("win32", exe, {"TEMP": str(tmp_path)}) is None


def _linux_target(tmp_path: Path) -> InstallTarget:
    return InstallTarget("linux", tmp_path / "PAM-Analyzer.AppImage", tmp_path / "stage")


def test_fetch_reuses_a_ready_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    staged = tmp_path / "stage" / "0.9.0" / "a.bin"
    monkeypatch.setattr(update_install, "ready_payload", lambda target, update: staged)
    monkeypatch.setattr(update_install, "download", lambda *a: pytest.fail("downloaded again"))
    assert fetch_payload(_linux_target(tmp_path), make_update(), "UA", no_progress, never) == staged


def test_fetch_reuse_only_returns_none_without_downloading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(update_install, "download", lambda *a: pytest.fail("downloaded without a click"))
    assert fetch_payload(_linux_target(tmp_path), make_update(), "UA", no_progress, never, reuse_only=True) is None


def test_fetch_removes_other_staged_versions(tmp_path: Path) -> None:
    (tmp_path / "stage" / "0.8.5").mkdir(parents=True)
    (tmp_path / "stage" / "0.9.0").mkdir()
    fetch_payload(_linux_target(tmp_path), make_update(), "UA", no_progress, never, reuse_only=True)
    assert [p.name for p in (tmp_path / "stage").iterdir()] == ["0.9.0"]


def test_fetch_downloads_into_the_version_dir_and_prepares(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    downloaded: list[Path] = []
    monkeypatch.setattr(update_install, "download", lambda update, dest, *_a: downloaded.append(dest))
    monkeypatch.setattr(update_install, "prepare", lambda target, update, archive: archive)
    payload = fetch_payload(_linux_target(tmp_path), make_update(), "UA", no_progress, never)
    assert downloaded == [tmp_path / "stage" / "0.9.0" / "a.bin"]
    assert payload == downloaded[0]


def test_fetch_prepares_a_verified_archive_again_instead_of_downloading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The DMG is intact but its extracted .app went missing."""
    archive = tmp_path / "stage" / "0.9.0" / "a.bin"
    prepared: list[Path] = []
    monkeypatch.setattr(update_install, "verified_archive", lambda target, update: archive)
    monkeypatch.setattr(update_install, "download", lambda *a: pytest.fail("downloaded again"))
    monkeypatch.setattr(update_install, "prepare", lambda target, update, a: prepared.append(a) or a)
    mac = InstallTarget("macos", tmp_path / "PAM-Analyzer.app", tmp_path / "stage")
    payload = fetch_payload(mac, make_update(), "UA", no_progress, never, reuse_only=True)
    assert prepared == [archive]
    assert payload == archive
