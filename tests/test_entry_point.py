"""The entry module must stay inert when a spawn worker re-executes it."""

import subprocess
import sys
import textwrap
from pathlib import Path

ENTRY = Path(__file__).parents[1] / "src" / "pam_analyzer" / "__main__.py"


def test_entry_module_is_inert_when_reexecuted_as_a_spawn_worker():
    """Re-executing the entry module under multiprocessing's worker name is a no-op.

    On a spawn start method a worker rebuilds the parent's state by executing
    the main module again, under the name __parents_main__. If the launch at
    the bottom of the module is not guarded, that worker starts a second GUI,
    which spawns workers of its own, and the machine dies. sys.frozen is not
    set in a Nuitka build, so multiprocessing.freeze_support() does not catch
    this. Run in a subprocess because the failure mode is os._exit().
    """
    script = textwrap.dedent("""
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location("__parents_main__", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        print("INERT")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(ENTRY)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "INERT" in result.stdout, (
        f"entry module ran its launch path when re-executed as a worker.\n"
        f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
