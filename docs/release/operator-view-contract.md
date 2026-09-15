# Durable operator read model

`OperatorView(CommandStore(...), expected_release=...)` reads one explicit
account/mode in a read-only, repeatable-read PostgreSQL transaction. It requires
execution schema 6 and control schema 1; it never initializes or migrates either.

The snapshot preserves canonical decimal strings, separate projection/database
timestamps, recovery and halt state, session controls, reconciliation, orders,
the latest 100 economic records and latest 50 commands and strategy decisions.
These bounded histories are labeled and are not a full ledger export. A missing
account raises an error; a no-trade account retains its real cash and empty lists.

A current worker lease means an account owner holds a lease, not that a trading
tick passed or a requested action succeeded. Commands separately preserve request,
acknowledgment and observation times. Historical success does not establish current
worker health. The database snapshot time is not a market quote or valuation time.

Submission requires a fresh lease readback with the expected release and passes
the caller's stable command ID to CommandStore. Duplicate identical requests retain
one identity. A lease can expire after the UI check; only the worker's own fencing
and release checks authorize execution. Operator aliases are metadata, not grants.

The view imports no Runtime or broker and loads no environment file. Its consumer
must show stale/unavailable data and distinct pending/recovery/success states. No
browser workflow or actual controller execution is claimed by the read model alone.
