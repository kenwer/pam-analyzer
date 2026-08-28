#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = ["birdnet>=1.1,<1.2"]
# ///
"""Fetch the Perch v2 ONNX weights the app runs on.

Unlike BirdNET v2.4, Perch cannot be converted during the build. Google ships it
as a jax2tf SavedModel whose whole graph sits inside XlaCallModule ops carrying
serialized StableHLO, and tf2onnx converts TensorFlow ops one at a time, of which
that graph has none. The route that does work runs through TFLite, whose MLIR
converter can see inside XlaCallModule, followed by manual graph surgery that its
author documents as partly hand-done. Reproducing that in this repo is not
realistic, so the app depends on the published artifact instead.

That makes provenance the thing to be careful about. The download is pinned to an
immutable Hugging Face commit rather than a branch, and verified by checksum
before it is installed, so a moved tag or a replaced file fails here rather than
silently changing what a campaign reports.

The no_dft variant is the one fetched. It replaces the model's DFT node with an
equivalent MatMul, which is faster in the two-threads-per-worker configuration
the runner uses, and its published tolerance is irrelevant at the precision the
app records. Measured over 400 segments of real recordings: no detection changes,
top-5 and rank-1 agree on every segment, and the worst Confidence difference is
1.0e-4, which is one unit in the last digit the CSV keeps.

Labels are not fetched. The export ships none, and the only upstream source is a
379 MB download the app would otherwise never need, so the label list is vendored
in the package instead.

Usage:
    uv run --script scripts/fetch_perch_onnx.py
    uv run --script scripts/fetch_perch_onnx.py --force
"""

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = "justinchuby/Perch-onnx"

# An immutable commit, not 'main'. The whole point of pinning is that the bytes
# cannot change under the app.
COMMIT = "da88bf8c37c0b3068fc706fe509f633b9fbd28c1"

FILENAME = "perch_v2_no_dft.onnx"
SHA256 = "4dcf71c18a147198545944bb5149697e89e3ad2e16637fa8f0edf6d13035a017"
SIZE_BYTES = 413_350_933

URL = f"https://huggingface.co/{REPO}/resolve/{COMMIT}/{FILENAME}"

CHUNK = 1 << 20

TIMEOUT_S = 60


def _model_path() -> Path:
    """Where the app expects the weights.

    Duplicates infrastructure.perch_onnx.model_path() because this script runs
    on its own interpreter and cannot import the app package. Resolved through
    birdnet's APP_DIR so it honours BIRDNET_APP_DATA the same way, which is what
    lets a frozen build fetch into its bundle. The two are pinned together by
    test_fetch_script_and_app_agree_on_the_model_path.
    """
    # Imported here rather than at module scope so --help works before the
    # dependency is resolved.
    from birdnet.utils.local_data import APP_DIR

    return APP_DIR / "acoustic-models" / "perch-v2" / "onnx" / "model-fp32.onnx"


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _download(dest: Path) -> None:
    """Stream the export to `dest`, reporting progress against the known size."""
    with urllib.request.urlopen(URL, timeout=TIMEOUT_S) as response, dest.open("wb") as out:  # noqa: S310
        done = 0
        while chunk := response.read(CHUNK):
            out.write(chunk)
            done += len(chunk)
            print(f"\r  {done / 1e6:7.1f} / {SIZE_BYTES / 1e6:.1f} MB", end="", flush=True)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even when the file verifies")
    args = parser.parse_args()

    out_path = _model_path()

    if not args.force and out_path.is_file():
        print(f"  Verifying {out_path.name}")
        if _digest(out_path) == SHA256:
            print("  Perch v2 onnx is current, skipping")
            return
        print("  Checksum mismatch, re-downloading")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="perch_onnx_") as tmp:
        staged = Path(tmp) / FILENAME
        print(f"  Fetching {FILENAME} from {REPO}@{COMMIT[:8]}")
        _download(staged)

        got = _digest(staged)
        if got != SHA256:
            # Installing an unverified 413 MB blob would put unknown weights
            # behind every detection the app writes, so this is fatal.
            raise SystemExit(f"checksum mismatch: expected {SHA256}, got {got}")

        # Move into place only once verified, so an interrupted run cannot
        # leave a partial file that later looks installed.
        shutil.move(str(staged), str(out_path))

    print(f"  Wrote {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    sys.exit(main())
