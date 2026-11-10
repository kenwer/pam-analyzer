import hashlib
from pathlib import Path

import pytest

from pam_analyzer.infrastructure.http_download import USER_AGENT, DownloadCancelled, DownloadError, download_file
from tests.infrastructure.conftest import Route, never, no_progress

BLOB = bytes(range(256)) * 20_000  # 5 MB, several download chunks
DIGEST = hashlib.sha256(BLOB).hexdigest()


def test_download_file_writes_verified_file(tmp_path: Path, local_http_server) -> None:
    local_http_server.routes["/m.onnx"] = Route(BLOB)
    dest = tmp_path / "models" / "m.onnx"
    download_file(local_http_server.url("/m.onnx"), len(BLOB), DIGEST, dest, no_progress, never)
    assert dest.read_bytes() == BLOB
    assert not dest.with_name("m.onnx.part").exists()


def test_download_file_mismatch_raises_download_error(tmp_path: Path, local_http_server) -> None:
    local_http_server.routes["/m.onnx"] = Route(BLOB)
    dest = tmp_path / "m.onnx"
    with pytest.raises(DownloadError):
        download_file(local_http_server.url("/m.onnx"), len(BLOB), "0" * 64, dest, no_progress, never)
    assert not dest.exists()
    assert not dest.with_name("m.onnx.part").exists()


def test_download_file_cancel_keeps_part(tmp_path: Path, local_http_server) -> None:
    local_http_server.routes["/m.onnx"] = Route(BLOB)
    dest = tmp_path / "m.onnx"
    with pytest.raises(DownloadCancelled):
        download_file(local_http_server.url("/m.onnx"), len(BLOB), DIGEST, dest, no_progress, lambda: True)
    assert dest.with_name("m.onnx.part").exists()


def test_download_file_http_404_raises_download_error(tmp_path: Path, local_http_server) -> None:
    with pytest.raises(DownloadError):
        download_file(local_http_server.url("/missing"), 10, DIGEST, tmp_path / "m.onnx", no_progress, never)


def test_download_error_names_the_host(tmp_path: Path, local_http_server) -> None:
    with pytest.raises(DownloadError, match="127.0.0.1"):
        download_file(local_http_server.url("/missing"), 10, DIGEST, tmp_path / "m.onnx", no_progress, never)


def test_unreachable_host_is_named(tmp_path: Path) -> None:
    with pytest.raises(DownloadError, match="nonexistent.invalid"):
        download_file("http://nonexistent.invalid/m.onnx", 10, DIGEST, tmp_path / "m.onnx", no_progress, never)


def test_checksum_error_names_the_host(tmp_path: Path, local_http_server) -> None:
    local_http_server.routes["/m.onnx"] = Route(BLOB)
    with pytest.raises(DownloadError, match="127.0.0.1"):
        download_file(local_http_server.url("/m.onnx"), len(BLOB), "0" * 64, tmp_path / "m.onnx", no_progress, never)


def _get(server, dest: Path, progress=no_progress) -> None:
    download_file(server.url("/m.onnx"), len(BLOB), DIGEST, dest, progress, never)


def test_download_file_sends_the_user_agent_and_reports_progress(tmp_path: Path, local_http_server) -> None:
    local_http_server.routes["/m.onnx"] = Route(BLOB)
    calls = []
    _get(local_http_server, tmp_path / "m.onnx", lambda d, t: calls.append((d, t)))
    assert local_http_server.requests[0]["User-Agent"] == USER_AGENT
    assert len(calls) > 1
    assert calls[-1] == (len(BLOB), len(BLOB))


def test_interrupted_download_resumes_with_range(tmp_path: Path, local_http_server) -> None:
    local_http_server.routes["/m.onnx"] = Route(BLOB)
    dest = tmp_path / "m.onnx"
    local_http_server.cut_after = 1_500_000
    with pytest.raises(DownloadError):
        _get(local_http_server, dest)
    assert dest.with_name("m.onnx.part").stat().st_size == 1_500_000
    _get(local_http_server, dest)
    assert local_http_server.requests[-1]["Range"] == "bytes=1500000-"
    assert dest.read_bytes() == BLOB


def test_server_ignoring_range_restarts_from_zero(tmp_path: Path, local_http_server) -> None:
    local_http_server.routes["/m.onnx"] = Route(BLOB)
    (tmp_path / "m.onnx.part").write_bytes(b"x" * 1000)
    local_http_server.honor_range = False
    _get(local_http_server, tmp_path / "m.onnx")
    assert (tmp_path / "m.onnx").read_bytes() == BLOB


def test_a_complete_part_file_is_verified_without_a_request(tmp_path: Path, local_http_server) -> None:
    (tmp_path / "m.onnx.part").write_bytes(BLOB)
    _get(local_http_server, tmp_path / "m.onnx")
    assert (tmp_path / "m.onnx").read_bytes() == BLOB
    assert local_http_server.requests == []


def test_a_complete_but_wrong_part_file_starts_over(tmp_path: Path, local_http_server) -> None:
    local_http_server.routes["/m.onnx"] = Route(BLOB)
    (tmp_path / "m.onnx.part").write_bytes(b"x" * len(BLOB))
    with pytest.raises(DownloadError):
        _get(local_http_server, tmp_path / "m.onnx")
    assert not (tmp_path / "m.onnx.part").exists()
    _get(local_http_server, tmp_path / "m.onnx")
    assert (tmp_path / "m.onnx").read_bytes() == BLOB
