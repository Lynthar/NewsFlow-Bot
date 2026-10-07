"""Adapters and the API reach the database only through services: a repository used
from either side skips the rules the service applies, as the REST feed delete once did."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "newsflow"


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


@pytest.mark.parametrize("layer", ["adapters", "api"])
def test_layer_does_not_import_repositories(layer):
    offenders = {
        str(path.relative_to(SRC)): sorted(
            m for m in _imported_modules(path) if m.startswith("newsflow.repositories")
        )
        for path in (SRC / layer).rglob("*.py")
    }
    assert {file: mods for file, mods in offenders.items() if mods} == {}
