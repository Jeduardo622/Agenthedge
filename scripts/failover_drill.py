"""Exercise runtime lease failover and checkpoint continuity on Postgres."""

from __future__ import annotations

import argparse
import json

from infra.postgres import ensure_postgres_schema, postgres_connection
from infra.runtime_state import PostgresRuntimeStateSink


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError("boolean is not a valid integer value in this context")
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError(f"cannot coerce {type(value).__name__} to int")


def _reset_tables(dsn: str) -> None:
    ensure_postgres_schema(dsn)
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE ah_runtime_leases RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE ah_runtime_checkpoints RESTART IDENTITY CASCADE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="Postgres DSN")
    args = parser.parse_args()

    _reset_tables(args.dsn)
    sink_a = PostgresRuntimeStateSink(
        args.dsn,
        instance_id="drill-a",
        profile="staging",
        backend="postgres",
    )
    sink_b = PostgresRuntimeStateSink(
        args.dsn,
        instance_id="drill-b",
        profile="staging",
        backend="postgres",
    )
    acquired_a, token_a = sink_a.acquire_lease(runtime_name="drill-runtime", lease_seconds=30)
    acquired_b, token_b = sink_b.acquire_lease(runtime_name="drill-runtime", lease_seconds=30)
    if not acquired_a or acquired_b:
        raise RuntimeError(
            "failover drill failed: expected first instance to lead and second to be fenced"
        )
    sink_a.save_checkpoint(
        runtime_name="drill-runtime",
        fence_token=token_a,
        tick_count=5,
        bus_checkpoint=9,
        kill_switch_reason=None,
        kill_switch_trigger=None,
        payload={"phase": "primary"},
    )
    sink_a.release_lease(runtime_name="drill-runtime", fence_token=token_a)
    acquired_b2, token_b2 = sink_b.acquire_lease(runtime_name="drill-runtime", lease_seconds=30)
    if not acquired_b2:
        raise RuntimeError(
            "failover drill failed: secondary instance could not acquire released lease"
        )
    checkpoint = sink_b.load_checkpoint(runtime_name="drill-runtime")
    if not checkpoint or _as_int(checkpoint.get("tick_count", -1)) != 5:
        raise RuntimeError("failover drill failed: checkpoint continuity mismatch")
    print(
        json.dumps(
            {
                "status": "ok",
                "initial_token": token_a,
                "fenced_secondary_token": token_b,
                "failover_token": token_b2,
                "checkpoint_tick_count": checkpoint.get("tick_count"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
