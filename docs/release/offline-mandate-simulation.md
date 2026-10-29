# Offline installed paper-mandate simulation

These tests exercise the installed paper worker and its actual mandate, durable
journal, risk service, runtime data checks and submission boundary. They use newly
constructed synthetic observations and replace broker/market-data HTTP transport.
No provider credentials, paid subscription, external broker account or real orders
are required. Synthetic `paper_broker` namespaces are test state, not observed paper
sessions or release authority.

## Scope and limits

`BacktestEngine` remains a separate generic historical replay. It does not install
the full paper mandate and cannot establish its aggregate pending-order limits.
Use `tests/integration/test_paper_mandate_simulation.py` for the installed path,
together with the maintained mandate, inventory, execution and construction tests.

The optional strict fixture is built before its test trust/configuration identities
are frozen. It retains the five-second quote/mark and risk-artifact budgets,
24-hour classification/liquidity freshness, maximum 0.001 spread fraction, and the
existing production risk policy. Its mandate permits only SPY/Momentum, a $10,000
allocation, one whole share/$1,000 per order and owned position, one outstanding
order, and the existing concentration/gross caps. Unrelated inventory is never
adopted, and a negative signal cannot authorize a short sale.

The fixture's single-strategy council support setting is explicit synthetic branch
coverage. It does not change or qualify operational consensus settings. Test
reviewer keys and signed fixture artifacts are not real G0–G2 attestations.

The scenarios check pending reservations across ticks, unresolved submissions
across worker reconstruction, terminal cancellation before slot release, owned
fill/position limits and owned sell behavior, and mandate bytes bound to the
approved identity. Retain test failures; a skipped database fixture is not proof.

In broker modes, runtime tick ordering completes Execution's existing
reconciliation before Director can publish new decisions. Reconciliation marks
its proof incomplete while it runs, and the journal conservatively retains
reservations during that interval. Starting it concurrently with Quant could
therefore block a correctly terminal order's successor. The runtime uses a stable
local ordering for broker ticks; all other agent relative ordering and the
simulated-mode order remain unchanged. Initial reconciliation, admission checks,
failure handling and the existing causal wait deadline remain in force.

Final paper-mandate validation also uses the existing locked submission-claim
proof. A new pristine claim is the only permitted addition to the last covered
revision. The validator binds the durable candidate to its original reservation
and installed mandate, applies allocation-scoped accounting and the covered prior
orders, then checks the same mandate limits. General reservation reads remain
conservative when a proof is incomplete or its revision changes. Another unknown
order, changed candidate or unqualified proof cannot be excluded by this path.

## Run locally

Use a newly created, owned, disposable PostgreSQL 16 instance bound to loopback.
Supply `WORKER_BUILDER_TEST_POSTGRES_DSN` only for that instance and set
`PYTHON_DOTENV_DISABLED=1`. Never point these tests at an operational database.
The fixtures create and migrate synthetic test schemas/accounts.

```text
poetry run pytest tests/agents/test_audit_chain.py
poetry run pytest tests/agents/test_runtime_reconciliation_order.py
poetry run pytest tests/integration/test_paper_mandate_simulation.py
poetry run pytest tests/integration/test_paper_mandate_claim.py
poetry run pytest tests/portfolio/test_paper_mandate.py tests/portfolio/test_paper_mandate_construction.py tests/integration/test_paper_mandate_install.py tests/integration/test_paper_mandate_inventory.py tests/integration/test_paper_mandate_execution.py
```

The additional maintained integration fixtures require their own explicitly
disposable database variables. `scripts/run_postgres_suite.py` supplies the full
isolated map for the complete suite; follow its clean-checkout, loopback-only,
PostgreSQL-client and new-evidence-directory guards.

## Windows cross-process audit test

`tests/agents/test_audit_chain.py` coordinates independent spawned writers through
separate pipes. Both sinks must report ready before either receives the append
release. It retains the original deadlines, child-exit checks, two expected event
types and full hash-chain verification. A fault-injection case establishes that
the test does not depend on multiprocessing Event semaphores.

This removes an unnecessary test-coordination dependency implicated by an observed
Windows `PermissionError` before audit append. It does not establish an operating
system root cause, change Windows permissions, alter production audit locking or
certify all Windows IPC behavior.

Passing these scenarios is software/synthetic evidence only. Historical strategy
performance, current market entitlement, observed paper sessions and the actual
reviewed release dossier remain separate requirements.
