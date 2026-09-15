"""Export attribution from an explicit journal namespace; never activate weights."""

import argparse
import json
import os
from pathlib import Path

from learning.replay import export_journal_attribution
from portfolio.journal import PostgresJournal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn-env", required=True, help="Existing environment variable holding DSN"
    )
    parser.add_argument("--account", required=True)
    parser.add_argument("--mode", required=True, choices=("simulated", "paper_broker", "live"))
    parser.add_argument(
        "--output", required=True, type=Path, help="New JSON file; parent must exist"
    )
    args = parser.parse_args()
    try:
        dsn = os.environ[args.dsn_env]
        report = export_journal_attribution(
            PostgresJournal(dsn), account_id=args.account, mode=args.mode
        )
        encoded = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
        with args.output.open("x", encoding="utf-8") as output:
            output.write(encoded)
    except Exception as exc:
        # Connection errors can contain credentials. Never echo raw provider errors.
        parser.exit(1, f"Attribution export failed ({type(exc).__name__}); no weights activated.\n")
    print(f"Attribution exported at checkpoint {report['checkpoint']}: {args.output}")


if __name__ == "__main__":
    main()
