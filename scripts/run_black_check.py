"""Custom Black check that bypasses the Python 3.12.5 CLI guard."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

import black

REPO_ROOT = Path(__file__).resolve().parents[1]
INCLUDE_DIRS = ("src", "tests", "scripts")
EXCLUDE_PARTS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".cache",
    "build",
    "dist",
}


def iter_python_files() -> Iterable[Path]:
    for relative in INCLUDE_DIRS:
        base = REPO_ROOT / relative
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if any(part in EXCLUDE_PARTS for part in path.parts):
                continue
            yield path


def main() -> None:
    mode = black.Mode(line_length=100, target_versions={black.TargetVersion.PY312})
    failures: list[Path] = []
    for file_path in iter_python_files():
        changed = black.format_file_in_place(
            file_path,
            fast=False,
            mode=mode,
            write_back=black.WriteBack.CHECK,
        )
        if changed:
            failures.append(file_path)
    if failures:
        print("Black formatting would change the following files:")
        for path in failures:
            print(f" - {path.relative_to(REPO_ROOT)}")
        sys.exit(1)
    print("Black style check passed via custom runner (Python 3.12.5 workaround).")


if __name__ == "__main__":
    main()
