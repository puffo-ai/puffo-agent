"""Fail when Python modules or callables exceed repository size limits."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FILE_LINE_LIMIT = 2_000
CALLABLE_LINE_LIMIT = 100
EXCLUDED_DIRECTORIES = {".git", ".venv", "build", "dist"}


def _python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if not EXCLUDED_DIRECTORIES.intersection(path.relative_to(root).parts):
            yield path


def _callable_violations(path: Path, source: str):
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        line_count = (node.end_lineno or node.lineno) - node.lineno + 1
        if line_count > CALLABLE_LINE_LIMIT:
            yield (
                node.lineno,
                f"{node.name} is {line_count} lines (limit {CALLABLE_LINE_LIMIT})",
            )


def _file_violations(root: Path, path: Path):
    source = path.read_text(encoding="utf-8")
    relative_path = path.relative_to(root)
    line_count = len(source.splitlines())
    if line_count > FILE_LINE_LIMIT:
        yield relative_path, 1, f"file is {line_count} lines (limit {FILE_LINE_LIMIT})"
    try:
        for line, message in _callable_violations(path, source):
            yield relative_path, line, message
    except SyntaxError as error:
        yield relative_path, error.lineno or 1, f"cannot parse: {error.msg}"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations = [
        violation
        for path in _python_files(root)
        for violation in _file_violations(root, path)
    ]
    for path, line, message in violations:
        print(f"{path}:{line}: {message}")
    if violations:
        print(f"Python structural check failed: {len(violations)} violation(s).")
        return 1
    print("Python structural check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
