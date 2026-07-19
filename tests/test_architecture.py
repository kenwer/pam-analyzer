"""Enforces the layer import rules documented in ARCHITECTURE.md.

The table there states which layer may import which. This test walks the
import statements of every module under src/pam_analyzer and fails on any
violation, so the layering cannot erode one convenient import at a time.
Update ALLOWED_INTERNAL together with the ARCHITECTURE.md table if the
rules ever change; they are two views of the same contract.
"""

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "pam_analyzer"
PACKAGE = "pam_analyzer"

# Internal layers each layer may import, besides itself. "app" is the
# composition root and may import everything, so it is exempt below.
ALLOWED_INTERNAL: dict[str, set[str]] = {
    "domain": set(),
    "infrastructure": {"domain"},
    "workers": {"domain", "infrastructure"},
    "widgets": {"domain"},
    "ui": {"domain", "infrastructure", "workers", "widgets"},
}

QT_MODULES = {"PySide6", "shiboken6"}
QT_FORBIDDEN_LAYERS = {"domain", "infrastructure"}


def _layer_of(file: Path) -> str:
    rel = file.relative_to(SRC)
    if len(rel.parts) == 1:
        # Root-level modules (__init__.py, __main__.py) are entry shims;
        # treat them like the composition root.
        return "app"
    return rel.parts[0]


def _imported_modules(file: Path) -> list[str]:
    """Absolute dotted module names imported by file, relative imports resolved."""
    tree = ast.parse(file.read_text(encoding="utf-8"))
    package_parts = (PACKAGE, *file.relative_to(SRC).parts[:-1])
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                modules.append(node.module or "")
            else:
                base = package_parts[: len(package_parts) - node.level + 1]
                if node.module:
                    modules.append(".".join((*base, node.module)))
                else:
                    # 'from . import x': each imported name may be a submodule.
                    modules.extend(".".join((*base, a.name)) for a in node.names)
    return modules


def test_layer_import_rules() -> None:
    violations: list[str] = []
    for file in sorted(SRC.rglob("*.py")):
        layer = _layer_of(file)
        if layer == "app":
            continue
        allowed = ALLOWED_INTERNAL[layer] | {layer}
        rel_name = file.relative_to(SRC).as_posix()
        for module in _imported_modules(file):
            top = module.split(".")[0]
            if top == PACKAGE:
                target = module.split(".")[1] if "." in module else ""
                if target and target not in allowed:
                    violations.append(f"{rel_name}: {layer} imports {module}")
            elif top in QT_MODULES and layer in QT_FORBIDDEN_LAYERS:
                violations.append(f"{rel_name}: {layer} imports Qt ({module})")
            elif layer == "domain" and top not in sys.stdlib_module_names:
                violations.append(f"{rel_name}: domain imports non-stdlib {module}")
    assert not violations, "Layer rule violations:\n" + "\n".join(violations)
