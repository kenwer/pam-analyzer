"""scripts/prewarm_models.py fills a model cache through the same store the app uses."""

import hashlib
from pathlib import Path

import pytest

from pam_analyzer.domain import ModelInfo
from pam_analyzer.infrastructure.http_download import DownloadError
from pam_analyzer.infrastructure.model_store import Artifact, ModelPackage, missing
from scripts.prewarm_models import prewarm
from tests.infrastructure.conftest import Route

_BODY = b"weights" * 1000
_INFO = ModelInfo("Stub 1.0", "https://example.org", "CC0")


def test_prewarm_installs_every_package_once(tmp_path: Path, local_http_server) -> None:
    local_http_server.routes["/w"] = Route(_BODY)
    artifact = Artifact("w", local_http_server.url("/w"), len(_BODY), hashlib.sha256(_BODY).hexdigest(), tmp_path / "w")
    prepared: list[str] = []
    prewarm(ModelPackage(_INFO, lambda: (artifact,), lambda n=n: prepared.append(n)) for n in "ab")
    assert missing([artifact]) == []
    assert prepared == ["a", "b"]
    assert len(local_http_server.requests) == 1


def test_prewarm_retries_a_failed_install() -> None:
    attempts: list[int] = []

    def flaky_prepare() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("connection reset")

    prewarm([ModelPackage(_INFO, lambda: (), flaky_prepare)], retry_delay_s=0)
    assert len(attempts) == 2


def test_prewarm_gives_up_after_three_attempts() -> None:
    def broken() -> None:
        raise RuntimeError("gone")

    with pytest.raises(DownloadError, match="gone"):
        prewarm([ModelPackage(_INFO, lambda: (), broken)], retry_delay_s=0)
