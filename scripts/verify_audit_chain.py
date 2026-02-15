"""Validate runtime audit hash-chain integrity."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from audit import verify_jsonl_hash_chain


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify audit JSONL hash-chain integrity.")
    parser.add_argument(
        "--path",
        default="storage/audit/runtime_events.jsonl",
        help="Path to JSONL audit log (default: storage/audit/runtime_events.jsonl)",
    )
    args = parser.parse_args()
    target = Path(args.path)
    ok, errors = verify_jsonl_hash_chain(target)
    if ok:
        print(f"Audit chain valid: {target}")
        return 0
    print(f"Audit chain invalid: {target}")
    for error in errors:
        print(f" - {error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
