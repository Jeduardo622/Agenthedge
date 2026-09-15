# Recovery drill evidence

## HTTP acceptance crash

`scripts/qualify_runtime.py` runs the maintained process-termination test through
pytest and writes a sanitized JSON report. Set `E5B2_TEST_POSTGRES_DSN` explicitly
to an exclusive local database whose name starts with `qualification_`, then run:

```text
poetry run python scripts/qualify_runtime.py --disposable-database --output .cache/completion/acceptance-crash.json
```

The database must be newly provisioned for qualification. Fixtures explicitly
initialize schema6 and use unique synthetic account namespaces. Never supply an
existing trading database. The driver accepts only loopback or Docker-host database
addresses, disables dotenv and external alerts, and makes no real broker request.
It rejects every database with any user relation and holds a database advisory
lease until the test process finishes. A completed run's database cannot be reused
by the driver; preserve it for inspection and provision a new one for the next run.
It refuses an existing output path. Run from a clean reviewed source checkout.

The test starts a real child using actual ExecutionAgent admission, PostgreSQL
journal, risk and release test fixtures. A loopback HTTP server accepts one order;
the child then blocks before recording the observation. The parent checks the
durable unknown state, forcibly terminates the child, and starts reconciliation
against the same provider order identity. An accepted order retains its reservation.
A subsequent synthetic fill produces one cash/position/checkpoint effect across
repeated reconciliation. Reclaiming the original submission remains prohibited.

Reports bind source SHA, dirty-tree status, the forced safe configuration hash,
OS, actual timestamps, elapsed time and pytest outcome. Nonzero exit, failed,
malformed or missing results fail. Skips remain blocked. A dirty source run exits
nonzero even if its test passes. Raw exception text, DSNs and account identifiers
are excluded from the report. Test-only diagnostic logs are temporary.

This drill is synthetic transport proof, not a real broker observation, host
power-loss guarantee or platform qualification. Database backup/restore,
worker fencing, scheduler recovery, cancellation uncertainty and operational
deployment evidence are separate required drills. The report explicitly retains
`platform_qualified=false`.

## Journal transaction and PostgreSQL restore

Provision two distinct, empty local databases whose names start with
`qualification_`. Set the source as `O4_SOURCE_TEST_POSTGRES_DSN` and the destination
as `O4_RESTORE_TEST_POSTGRES_DSN`. Put compatible PostgreSQL `pg_dump` and
`pg_restore` executables on PATH. They connect using those exact DSNs; a separate
container or database target is not selected. Then run:

```text
poetry run python scripts/qualify_runtime.py --drill journal-restore --disposable-database --output .cache/completion/journal-restore.json
```

The maintained test exclusively leases both databases and refuses either after any
user relation exists, including when invoked directly through pytest. The source
and destination must use distinct names, explicit loopback hosts, and no service
or alternate host-address override. The test terminates real Python child processes before
and after the journal transaction commit boundary. It replays the same canonical
trade, then uses `pg_dump` and `pg_restore`. The restored
schema6/control1 database must exactly reproduce Decimal cash, positions and cost,
economic checkpoint, order posted quantity/value/fee, remaining reservations,
outbox records, and operational snapshot timestamps. Replaying the event after
restore must be a deduplicated no-op.

This is a disposable synthetic database and process-termination drill. It records
measured test and driver durations without asserting an unapproved recovery target.
It does not establish storage durability under host power loss, restore an existing
database, contact a broker, or set `platform_qualified=true`.
