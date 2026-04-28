#!/usr/bin/env python
"""Compile Qt resource (.qrc) files to *_rc.py modules.

Usage:
    python scripts/compile_qrc.py

This script is cross-platform and avoids shell-style argument quoting issues
that occur with `python -c "..."` on Windows.
"""

import subprocess
from pathlib import Path


def main():
    root_dir = Path(__file__).parent.parent
    ui_dir = root_dir / "src" / "pam_analyzer" / "ui"

    for f in ui_dir.rglob("*.qrc"):
        if f.name.startswith("._"):
            # Skip macOS resource fork files (._*)
            continue

        out = f.with_name(f.stem + "_rc.py")
        subprocess.run(
            [
                "pyside6-rcc",
                "--format-version",
                "1",
                str(f),
                "-o",
                str(out),
            ],
            check=True,
        )
        print(f"Compiled: {f.relative_to(root_dir)} -> {f.stem}_rc.py")


if __name__ == "__main__":
    main()
