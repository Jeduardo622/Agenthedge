"""Split mixed audit JSONL into unhashed and chained archives."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _is_chained(record: dict[str, Any]) -> bool:
    claimed_hash = record.get("hash")
    prev_hash = record.get("prev_hash")
    return (
        isinstance(claimed_hash, str)
        and bool(claimed_hash)
        and "prev_hash" in record
        and (prev_hash is None or isinstance(prev_hash, str))
    )


def migrate_mixed_audit_log(source: Path, archive_dir: Path) -> tuple[Path, Path]:
    if not source.exists():
        raise FileNotFoundError(f"audit file not found: {source}")
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    legacy_path = archive_dir / f"legacy_unhashed_{stamp}.jsonl"
    chained_path = archive_dir / f"runtime_events_chained_{stamp}.jsonl"

    with (
        source.open("r", encoding="utf-8") as input_handle,
        legacy_path.open("w", encoding="utf-8") as legacy_handle,
        chained_path.open("w", encoding="utf-8") as chained_handle,
    ):
        for index, raw in enumerate(input_handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                legacy_handle.write(raw if raw.endswith("\n") else f"{raw}\n")
                continue
            if not isinstance(record, dict):
                legacy_handle.write(raw if raw.endswith("\n") else f"{raw}\n")
                continue
            target = chained_handle if _is_chained(record) else legacy_handle
            target.write(raw if raw.endswith("\n") else f"{raw}\n")
            if index % 10000 == 0:
                target.flush()
    return legacy_path, chained_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Split mixed audit log into legacy/chained archives."
    )
    parser.add_argument(
        "--source",
        default="storage/audit/runtime_events.jsonl",
        help="Source audit file to split.",
    )
    parser.add_argument(
        "--archive-dir",
        default="storage/audit/archive",
        help="Archive directory for split outputs.",
    )
    args = parser.parse_args()
    source = Path(args.source)
    archive_dir = Path(args.archive_dir)
    legacy_path, chained_path = migrate_mixed_audit_log(source, archive_dir)
    print(f"Legacy archive: {legacy_path}")
    print(f"Chained archive: {chained_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
