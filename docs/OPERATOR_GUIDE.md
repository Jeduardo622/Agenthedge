# Durable operator dashboard

Launch the durable read model with an explicit PostgreSQL DSN in the process
environment and an exact account, broker mode, release commit, and operator alias:

```powershell
$env:POSTGRES_DSN = '<explicit reviewed database DSN>'
poetry run python -m cli.dashboard --durable --account-id <account> --mode paper_broker --release <40-character-sha> --actor <operator-alias>
```

The durable branch never reads `.env` and never starts a local Runtime or broker.
It displays one repeatable-read account snapshot with the worker lease, portfolio,
positions, orders, reservations, bounded economic/fill history, session risk policy,
reconciliation, decisions, and command states. The JSON download is the same bounded
view, not a complete ledger export.

Commands stay disabled unless a current worker lease matches the expected release.
The UI reuses one command ID across reruns and accidental double clicks; choose **New
request identity** only for a deliberately new request. `PENDING`, `ACKNOWLEDGED /
OUTCOME UNCERTAIN`, `HALTING`, and `RECOVERY REQUIRED` remain distinct. Every request
reloads durable state. A live request additionally requires typing the exact account,
mode, and release identity shown by the UI. This confirmation records intent; worker
release gates and fencing still decide whether an action is authorized and applied.
