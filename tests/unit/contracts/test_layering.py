from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[3] / "src" / "agentic_saga"
_LEGACY = frozenset({"execution", "kernel", "storage"})


def _agentic_imports(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text())
    names = (_module(node) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
    return frozenset(name for name in names if name is not None)


def _module(node: ast.ImportFrom) -> str | None:
    if node.module is None or not node.module.startswith("agentic_saga."):
        return None
    return node.module.split(".", maxsplit=2)[1]


def test_temporal_layer_depends_only_on_retained_library_layers() -> None:
    imports = set[str]()
    for path in (ROOT / "temporal").glob("*.py"):
        imports.update(_agentic_imports(path))

    assert not imports & _LEGACY
    assert imports <= {"contracts", "temporal"}


def test_legacy_runtime_layers_are_not_shipped() -> None:
    shipped = {path.parent.name for path in ROOT.glob("*/*.py")}

    assert not shipped & _LEGACY
