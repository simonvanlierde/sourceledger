"""The package's modules depend downward only, and only `fetch` touches the network."""

import ast
from pathlib import Path

import pytest
from support import build_mod

from sourceledger import manifest

SRC = Path(manifest.__file__).parent
# A module imports only from a lower level. `fetch` and `build` do not import each other.
LEVEL = {"manifest": 0, "ledger": 1, "fetch": 2, "build": 2, "report": 3, "cli": 4}
NETWORK = {"socket", "ssl", "http", "urllib.request", "urllib.error", "urllib.response"}


def _imports(module: str) -> list[ast.Import | ast.ImportFrom]:
    tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]


def _modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    # `from urllib import request` imports urllib.request.
    return [node.module or "", *(f"{node.module}.{alias.name}" for alias in node.names)]


@pytest.mark.parametrize("module", LEVEL)
def test_a_module_imports_only_from_a_lower_level(module: str) -> None:
    for node in _imports(module):
        for name in _modules(node):
            _, _, sibling = name.partition("sourceledger.")
            if name.startswith("sourceledger.") and sibling.split(".")[0] in LEVEL:
                assert LEVEL[sibling.split(".")[0]] < LEVEL[module], f"{module} imports {name}"


@pytest.mark.parametrize("module", ["manifest", "ledger", "build", "report"])
def test_only_fetch_and_the_cli_reach_the_network(module: str) -> None:
    for node in _imports(module):
        for name in _modules(node):
            assert name not in NETWORK, f"{module} imports {name}"


def test_the_cli_uses_only_public_names_of_the_package() -> None:
    for node in _imports("cli"):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sourceledger."):
            private = [alias.name for alias in node.names if alias.name.startswith("_")]
            assert not private, f"cli imports {private} from {node.module}"


def test_the_manifest_knows_the_formats_build_reads() -> None:
    assert set(manifest._FORMATS) == set(build_mod._PARSERS)  # noqa: SLF001
