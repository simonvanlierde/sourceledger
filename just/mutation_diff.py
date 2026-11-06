# ruff: noqa: INP001, S603, S607, T201
"""Print the mutmut patterns for the functions a diff changes, one per line.

Used by `just py-mutation-diff`. Takes the git ref to diff against (default origin/main)
and reads [tool.mutmut] from pyproject.toml in the current directory. Only functions and
methods in the mutated modules count: mutmut names a function `<module>.x_<name>__mutmut_<n>`
and a method `<module>.xǁ<Class>ǁ<name>__mutmut_<n>`.
"""

import ast
import fnmatch
import re
import subprocess
import sys
import tomllib
from pathlib import Path

HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def changed_lines(path: str, base: str, *, untracked: bool) -> set[int]:
    """Line numbers in the new file that the diff adds or touches; all of them when untracked."""
    if untracked:
        return set(range(1, len(Path(path).read_text().splitlines()) + 1))
    diff = subprocess.run(
        ["git", "diff", "-U0", "--relative", base, "--", path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    lines: set[int] = set()
    for start, count in HUNK.findall(diff):
        # A pure deletion has count 0: mark the line it sits next to.
        lines.update(range(int(start), int(start) + max(int(count or 1), 1)))
    return lines


def functions(tree: ast.Module, module: str) -> list[tuple[range, str]]:
    """Line range and mutmut pattern of each top-level function and method.

    mutmut leaves a function alone when it has any decorator but staticmethod or classmethod.
    """
    found: list[tuple[range, str]] = []
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        members = [(None, node)] if isinstance(node, funcs) else []
        if isinstance(node, ast.ClassDef):
            members = [(node.name, m) for m in node.body if isinstance(m, funcs)]
        for owner, func in members:
            decorators = {ast.unparse(d) for d in func.decorator_list}
            if decorators - {"staticmethod", "classmethod"}:
                continue
            first = min([func.lineno, *(d.lineno for d in func.decorator_list)])
            name = f"xǁ{owner}ǁ{func.name}" if owner else f"x_{func.name}"
            found.append((range(first, (func.end_lineno or func.lineno) + 1), f"{module}.{name}__mutmut_*"))
    return found


def main(base: str) -> None:
    """Print one pattern per changed function."""
    config = tomllib.loads(Path("pyproject.toml").read_text()).get("tool", {}).get("mutmut", {})
    roots = config.get("source_paths") or ["src", "lib"]
    only = config.get("only_mutate")
    skip = config.get("do_not_mutate", [])
    names = subprocess.run(
        ["git", "diff", "--relative", "--name-only", base, "--", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    for name in [*names, *untracked]:
        path = Path(name)
        if not path.exists() or not any(path.is_relative_to(r) for r in roots):
            continue
        # mutmut matches both lists as globs on the path.
        if only and not any(fnmatch.fnmatch(name, p) for p in only):
            continue
        if any(fnmatch.fnmatch(name, p) for p in skip):
            continue
        # src/pkg/mod.py is module pkg.mod; so is pkg/mod.py with source_paths = ["pkg"].
        # mutmut strips only a leading "src".
        parts = path.with_suffix("").parts
        module = ".".join(parts[1:] if parts[:1] == ("src",) else parts).removesuffix(".__init__")
        touched = changed_lines(name, base, untracked=name in untracked)
        for lines, pattern in functions(ast.parse(path.read_text()), module):
            if touched & set(lines):
                print(pattern)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "origin/main")
