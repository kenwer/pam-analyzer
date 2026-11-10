import hashlib
import os
import sys
from pathlib import Path

import pytest

from pam_analyzer.domain import ModelInfo
from pam_analyzer.infrastructure.http_download import DownloadCancelled, DownloadError
from pam_analyzer.infrastructure.model_store import (
    Artifact,
    ModelPackage,
    download,
    install,
    marker_path,
    missing,
    missing_bytes,
)
from tests.infrastructure.conftest import Route, never, no_progress

BLOB_A = bytes(range(256)) * 8_000  # 2 MB, two download chunks
BLOB_B = bytes(reversed(range(256))) * 4_000


def _serve(server, path: str, body: bytes, dest: Path, **kwargs) -> Artifact:
    server.routes[path] = Route(body)
    return Artifact(
        id=path.strip("/"),
        url=server.url(path),
        size=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        dest=dest,
        **kwargs,
    )


def test_missing_lists_absent_artifact(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    assert missing([a]) == [a]
    assert missing_bytes([a]) == len(BLOB_A)


def test_file_without_marker_is_missing(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    a.dest.write_bytes(BLOB_A)
    assert missing([a]) == [a]


def test_marker_with_other_sha_is_missing(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    a.dest.write_bytes(BLOB_A)
    marker_path(a).write_text("f" * 64)
    assert missing([a]) == [a]


def test_download_writes_marker_and_file(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "models" / "a.onnx")
    download([a], no_progress, never)
    assert a.dest.read_bytes() == BLOB_A
    assert marker_path(a).read_text().strip() == a.sha256
    assert missing([a]) == []
    assert missing_bytes([a]) == 0


def test_shared_artifact_downloaded_once(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    assert missing_bytes([a, a]) == len(BLOB_A)
    download([a, a], no_progress, never)
    assert [r["path"] for r in local_http_server.requests] == ["/a"]


def test_partial_failure_keeps_finished_artifacts(tmp_path: Path, local_http_server) -> None:
    a1 = _serve(local_http_server, "/a1", BLOB_A, tmp_path / "a1.onnx")
    a2 = Artifact(id="a2", url=local_http_server.url("/gone"), size=10, sha256="0" * 64, dest=tmp_path / "a2.onnx")
    with pytest.raises(DownloadError):
        download([a1, a2], no_progress, never)
    assert missing([a1, a2]) == [a2]


def test_cancel_keeps_part_for_resume(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    with pytest.raises(DownloadCancelled):
        download([a], no_progress, lambda: True)
    assert missing([a]) == [a]
    download([a], no_progress, never)
    assert local_http_server.requests[-1].get("Range", "").startswith("bytes=")
    assert missing([a]) == []


def test_stale_siblings_deleted(tmp_path: Path, local_http_server) -> None:
    stale = tmp_path / "model-fp32-aaaaaaaaaaaa.onnx"
    stale.write_bytes(b"old")
    labels = tmp_path / "labels.txt"
    labels.write_text("keep")
    a = _serve(
        local_http_server, "/a", BLOB_A, tmp_path / "model-fp32-bbbbbbbbbbbb.onnx", stale_glob="model-fp32*.onnx"
    )
    download([a], no_progress, never)
    assert not stale.exists()
    assert a.dest.exists()
    assert labels.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_write_error_fails_without_marker(tmp_path: Path, local_http_server) -> None:
    folder = tmp_path / "ro"
    folder.mkdir()
    a = _serve(local_http_server, "/a", BLOB_A, folder / "a.onnx")
    os.chmod(folder, 0o500)
    try:
        with pytest.raises(DownloadError):
            download([a], no_progress, never)
    finally:
        os.chmod(folder, 0o700)
    assert not marker_path(a).exists()


def test_progress_spans_whole_batch(tmp_path: Path, local_http_server) -> None:
    a1 = _serve(local_http_server, "/a1", BLOB_A, tmp_path / "a1.onnx")
    a2 = _serve(local_http_server, "/a2", BLOB_B, tmp_path / "a2.onnx")
    calls: list[tuple[int, int]] = []
    download([a1, a2], lambda d, t: calls.append((d, t)), never)
    total = len(BLOB_A) + len(BLOB_B)
    assert calls[-1] == (total, total)
    assert all(t == total for _, t in calls)


def test_existing_verified_file_is_adopted_without_download(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    a.dest.write_bytes(BLOB_A)
    download([a], no_progress, never)
    assert local_http_server.requests == []
    assert missing([a]) == []


def test_existing_wrong_file_is_replaced(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    a.dest.write_bytes(b"stale weights")
    download([a], no_progress, never)
    assert a.dest.read_bytes() == BLOB_A


_INFO = ModelInfo("Stub 1.0", "https://example.org", "CC0")


def test_install_downloads_then_prepares(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    seen: list[list[Artifact]] = []
    install(ModelPackage(_INFO, lambda: (a,), lambda: seen.append(missing([a]))), no_progress, never)
    assert seen == [[]]


def test_install_prepares_when_nothing_is_missing(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    download([a], no_progress, never)
    calls: list[int] = []
    install(ModelPackage(_INFO, lambda: (a,), lambda: calls.append(1)), no_progress, never)
    assert calls == [1]
    assert len(local_http_server.requests) == 1


def test_install_reports_a_failed_prepare_as_download_error(tmp_path: Path) -> None:
    def prepare() -> None:
        raise RuntimeError("bad label file")

    with pytest.raises(DownloadError, match="bad label file"):
        install(ModelPackage(_INFO, lambda: (), prepare), no_progress, never)


def test_download_reports_zero_before_the_first_byte(tmp_path: Path, local_http_server) -> None:
    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    calls: list[tuple[int, int]] = []
    download([a], lambda d, t: calls.append((d, t)), never)
    assert calls[0] == (0, len(BLOB_A))


def test_concurrent_installs_of_a_shared_artifact_fetch_it_once(tmp_path: Path, local_http_server) -> None:
    """v3.0 and Perch share files, and both may download in the background at once."""
    import threading

    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    errors: list[BaseException] = []

    package = ModelPackage(_INFO, lambda: (a,))

    def second() -> None:
        try:
            install(package, no_progress, never)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    other = threading.Thread(target=second)

    def first_progress(done: int, _total: int) -> None:
        # Mid-transfer, with the first download's .part half written
        if done > 0 and other.ident is None:
            other.start()
            other.join(timeout=0.3)  # waiting for the first install, or done with its own download

    install(package, first_progress, never)
    other.join()
    assert errors == []
    assert missing([a]) == []
    assert len(local_http_server.requests) == 1


def test_waiting_for_another_install_can_be_cancelled(tmp_path: Path, local_http_server) -> None:
    from pam_analyzer.infrastructure import model_store

    a = _serve(local_http_server, "/a", BLOB_A, tmp_path / "a.onnx")
    model_store._install_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(DownloadCancelled):
            install(ModelPackage(_INFO, lambda: (a,)), no_progress, lambda: True)
    finally:
        model_store._install_lock.release()  # noqa: SLF001


def test_prepare_never_runs_twice_at_once() -> None:
    import threading
    import time

    active: list[int] = []
    peak: list[int] = []

    def prepare() -> None:
        active.append(1)
        peak.append(len(active))
        time.sleep(0.05)
        active.pop()

    package = ModelPackage(_INFO, lambda: (), prepare)
    threads = [threading.Thread(target=install, args=(package, no_progress, never)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(peak) == 1
