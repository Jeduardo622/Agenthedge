"""Validate runtime audit hash-chain integrity."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit import verify_jsonl_hash_chain


def _write_report(
    report_dir: Path,
    *,
    target: Path,
    ok: bool,
    errors: list[str],
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    status = "ok" if ok else "failed"
    path = report_dir / f"audit_chain_report_{status}_{stamp}.json"
    payload: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "target_path": str(target),
        "ok": ok,
        "error_count": len(errors),
        "errors": errors,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify audit JSONL hash-chain integrity.")
    parser.add_argument(
        "--path",
        default="storage/audit/runtime_events.jsonl",
        help="Path to JSONL audit log (default: storage/audit/runtime_events.jsonl)",
    )
    parser.add_argument(
        "--report-dir",
        default="storage/audit/reports",
        help="Directory to write verification reports.",
    )
    parser.add_argument(
        "--skip-report",
        action="store_true",
        help="Do not write a report artifact.",
    )
    args = parser.parse_args()
    target = Path(args.path)
    ok, errors = verify_jsonl_hash_chain(target)
    report_path: Path | None = None
    if not args.skip_report:
        report_path = _write_report(Path(args.report_dir), target=target, ok=ok, errors=errors)
    if ok:
        print(f"Audit chain valid: {target}")
        if report_path:
            print(f"Report: {report_path}")
        return 0
    print(f"Audit chain invalid: {target}")
    for error in errors:
        print(f" - {error}")
    if report_path:
        print(f"Report: {report_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
