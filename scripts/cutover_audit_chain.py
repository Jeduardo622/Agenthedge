"""Archive current runtime audit log and start a fresh chained file."""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path


def cutover_audit_log(active_path: Path, archive_dir: Path) -> tuple[Path | None, Path]:
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived: Path | None = None
    if active_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archived = archive_dir / f"runtime_events_prehash_{stamp}.jsonl"
        shutil.move(str(active_path), archived)
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.touch(exist_ok=True)
    return archived, active_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive current runtime audit file and create a fresh active file."
    )
    parser.add_argument(
        "--active-path",
        default="storage/audit/runtime_events.jsonl",
        help="Active runtime audit file path.",
    )
    parser.add_argument(
        "--archive-dir",
        default="storage/audit/archive",
        help="Archive directory for previous audit logs.",
    )
    args = parser.parse_args()
    archived, active = cutover_audit_log(Path(args.active_path), Path(args.archive_dir))
    if archived:
        print(f"Archived previous audit log: {archived}")
    else:
        print("No previous active audit log found; created new active file.")
    print(f"Active audit log: {active}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
