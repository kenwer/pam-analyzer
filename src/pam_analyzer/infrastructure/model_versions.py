"""Pinned versions of externally hosted model checkpoints.

This module lives in its own file because the CI model-cache key hashes it
(see .github/workflows/build.yml). Bumping a version here invalidates the
cached model directory on CI without unrelated edits to birdnet_lib.py
forcing a multi-gigabyte re-download.
"""

# Kaggle version of google/bird-vocalization-classifier/tensorFlow2/perch_v2_cpu.
# Requesting a versioned handle lets kagglehub resolve the model from its
# local cache without contacting the Kaggle API, and protects the logit
# calibration in perch_runner.py from upstream weight updates. Must match
# what scripts/build.py prewarms into the bundle (it imports the same
# loader, so it does by construction).
PERCH_V2_KAGGLE_VERSION = 1
