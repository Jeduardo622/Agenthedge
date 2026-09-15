"""Explicit execution journal schema migration. Never loads dotenv or imports accounts."""

import argparse
import json
import os

from infra.postgres import migrate_execution_journal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--target-version", type=int, choices=(1, 2, 3, 4), default=4)
    args = parser.parse_args()
    dsn = os.environ.get("EXECUTION_MIGRATION_DSN")
    if not dsn:
        parser.error("EXECUTION_MIGRATION_DSN must explicitly identify the migration database")
    print(
        json.dumps(
            migrate_execution_journal(
                dsn, apply=args.apply, rollback=args.rollback, target_version=args.target_version
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
