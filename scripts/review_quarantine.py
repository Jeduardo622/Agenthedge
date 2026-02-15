"""Inspect and manage quarantined data records."""

from __future__ import annotations

import argparse
import json
import sys

from data.quarantine import QuarantineStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Review or release quarantined data entries.")
    parser.add_argument(
        "--path",
        default="storage/quarantine/quarantined_data.jsonl",
        help="Quarantine JSONL file path.",
    )
    parser.add_argument("--release-symbol", help="Symbol to release from quarantine.")
    parser.add_argument("--release-type", help="Data type to release (quote/fundamentals/news).")
    args = parser.parse_args()

    store = QuarantineStore(args.path)
    if args.release_symbol and args.release_type:
        store.release(symbol=args.release_symbol, data_type=args.release_type)
        print(f"Released {args.release_symbol}:{args.release_type}")
        return 0
    records = store.list_records(include_released=True)
    print(json.dumps(records, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
