"""Static guard: the Model Selection Engine must stay completely outside
the execution path. This parses the actual source of every file in
`hermes_v2.model_selection` with Python's `ast` module (not a runtime
import check, so it also catches an import that's present but unused)
and asserts none of the forbidden trading/execution modules are
imported anywhere, and that `TRADING_ENABLED` is never referenced.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

FORBIDDEN_MODULES = {
    "hermes_v2.integrations.binance",
    "hermes_v2.trading.order_service",
    "hermes_v2.trading.risk_engine",
}


def _package_source_files() -> list[Path]:
    package = importlib.import_module("hermes_v2.model_selection")
    package_dir = Path(package.__file__).parent
    return sorted(package_dir.glob("*.py"))


def _imported_module_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("source_file", _package_source_files(), ids=lambda p: p.name)
def test_file_imports_no_forbidden_execution_module(source_file: Path) -> None:
    tree = ast.parse(source_file.read_text(), filename=str(source_file))
    imported = _imported_module_names(tree)
    forbidden_hit = imported & FORBIDDEN_MODULES
    assert not forbidden_hit, (
        f"{source_file.name} imports forbidden module(s): {forbidden_hit}"
    )


@pytest.mark.parametrize("source_file", _package_source_files(), ids=lambda p: p.name)
def test_file_never_references_trading_enabled(source_file: Path) -> None:
    source = source_file.read_text()
    assert "TRADING_ENABLED" not in source, (
        f"{source_file.name} references TRADING_ENABLED."
    )


def test_forbidden_module_list_is_not_accidentally_empty() -> None:
    # A guard against the guard itself silently doing nothing.
    assert len(FORBIDDEN_MODULES) == 3


def test_package_has_source_files_to_check() -> None:
    assert len(_package_source_files()) >= 5
