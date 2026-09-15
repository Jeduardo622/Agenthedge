# Learning attribution and promotion

`scripts/replay_learning.py` reads one explicit journal account and mode in a
repeatable, read-only transaction. It exports canonical events, their original
intent owners, a content hash, checkpoint, exact attributed realized P&L and
unavailable owner IDs. It does not update a journal, performance file or weights.

For a provisioned disposable test database whose DSN is already supplied through
`PROJECTION_TEST_POSTGRES_DSN`, with the repository `src` directory on `PYTHONPATH`:

```powershell
python scripts/replay_learning.py --dsn-env PROJECTION_TEST_POSTGRES_DSN --account <explicit-account> --mode simulated --output <new-report.json>
```

The output parent must exist; an existing output is never overwritten. No `.env`
file is loaded. Connection errors are reported by exception type without a DSN.
For another authorized namespace, explicitly select its existing DSN variable,
account and mode. Unknown namespaces fail; missing original ownership remains
unavailable rather than being assigned to the exit strategy.
Accounts initialized with preexisting positions are rejected: the journal lacks
their original entry lots and owners, so complete attribution cannot be reconstructed.

Replaying `envelopes` through `PerformanceTracker.record_economic_event` into a
new isolated file reconstructs attribution. Corrections keep the original entry
owners. This reconstructs economic attribution only: previous feedback, confidence
observations, safety penalties and accepted active weights remain separate state.

Upward activation now requires a controller-owned `StrategyAcceptance`: immutable
manifest and signed evidence bytes, independent `ReleaseTrust`, and the controller
clock. The manifest hash must equal the trusted strategy hash and its
`strategy_weights` entry must equal the current candidate. Live activation checks
the live-start gates again; invalid, missing, changed or expired evidence cannot
activate a candidate. Two matching unverified hash strings are insufficient.

The operator controller must bind this authority to the actual loaded target
artifacts and account. The export and unit-test synthetic signatures grant no
paper or live qualification and make no claim about future strategy returns.

Safety reductions invalidate older upward acceptance through each strategy's
persisted `penalties` counter. A signed strategy manifest may include
`strategy_safety_revisions`, for example `{"momentum": 1, "value": 0}`. Each
entry must be a nonnegative integer equal to that strategy's current counter.
An omitted entry authorizes only revision zero. Duplicate feedback receipts do
not advance the counter twice; economic attribution rebuilds and accepted
promotions do not reset it. The accepted revision is saved with the active
candidate version. Restoring an earlier weight after a reduction therefore
requires newly signed artifact bytes (and a new accepted strategy hash), even
when its numeric weight matches an older approval. The controller must read
current counters when requesting that acceptance; it cannot infer approval from
feedback or reuse another strategy's revision.

Startup controllers use `PerformanceTracker.install_accepted_weights(acceptance)`
to install the complete signed strategy roster atomically. This initializes active
and candidate weights without manufacturing trades, feedback, or safety counters.
The file binds account and mode, approved caps, current strategy hash, original
accepted safety revisions, and retired hashes. Nonempty unbound legacy state needs
an explicit migration; it cannot be adopted implicitly. Canonical economic inputs
must match the installed namespace.

Reinstalling the same signed hash revalidates authority against its original
accepted revisions and preserves current candidates, counters, and lower active
weights. A different hash requires every strategy's current safety revision and
cannot omit previously installed strategies. Retired hashes cannot be restored.
The single-strategy activation API cannot bypass that whole-roster transition.
`to_dict()` returns this installation identity and caps; `installed_weights()`
returns the currently effective capped roster, or `None` for uninstalled replay.

Quant rejects enabled strategies missing from the installed roster. Fixed/custom
weights cannot raise installed active weights or override recorded safety
reductions in legacy simulated runs. These local installation semantics do not
replace the controller's independent loaded-artifact binding or release gates.
