#!/usr/bin/env python
"""Compile Qt Designer .ui files to ui_*.py modules.

Usage:
    python scripts/compile_ui.py

This script is cross-platform and avoids shell-style argument quoting issues
that occur with `python -c "..."` on Windows.
"""

import subprocess
from pathlib import Path


def main():
    root_dir = Path(__file__).parent.parent
    ui_dir = root_dir / "src" / "pam_analyzer" / "ui"

    for f in ui_dir.rglob("*.ui"):
        if f.name.startswith("._"):
            # Skip macOS resource fork files (._*)
            continue

        out = f.parent / f"ui_{f.stem}.py"
        subprocess.run(
            ["pyside6-uic", "--from-imports", str(f), "-o", str(out)],
            check=True,
        )
        print(f"Compiled: {f.relative_to(root_dir)} -> ui_{f.stem}.py")


if __name__ == "__main__":
    main()
