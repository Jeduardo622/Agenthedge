"""Validate packaged wheel contains and imports critical runtime modules."""

from __future__ import annotations

import argparse
import glob
import importlib
import sys
from pathlib import Path
from zipfile import ZipFile

REQUIRED_PREFIXES = ("ops/", "observability/")
REQUIRED_IMPORTS = ("ops.scheduler", "observability.state", "cli.runtime")


def _resolve_wheel(explicit: str | None) -> Path:
    if explicit:
        target = Path(explicit)
        if not target.exists():
            raise FileNotFoundError(f"wheel not found: {target}")
        return target
    candidates = sorted(glob.glob("dist/*.whl"))
    if not candidates:
        raise FileNotFoundError("no wheel found under dist/. Build one first.")
    return Path(candidates[-1])


def _verify_contents(wheel: Path) -> None:
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for prefix in REQUIRED_PREFIXES:
        if not any(name.startswith(prefix) for name in names):
            raise RuntimeError(f"wheel missing package prefix: {prefix}")


def _verify_imports(wheel: Path) -> None:
    sys.path.insert(0, str(wheel.resolve()))
    try:
        for module_name in REQUIRED_IMPORTS:
            importlib.import_module(module_name)
    finally:
        if sys.path and sys.path[0] == str(wheel.resolve()):
            sys.path.pop(0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", help="Path to wheel artifact. Defaults to latest dist/*.whl")
    args = parser.parse_args()

    wheel = _resolve_wheel(args.wheel)
    _verify_contents(wheel)
    _verify_imports(wheel)
    print(f"package smoke check passed: {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
