# Codex Repository Instructions

This file is the repository-level operating guide for Codex and other engineering agents working in Agenthedge.

## Scope

- These instructions apply to the whole repository unless a more specific `AGENTS.md` exists in a subdirectory.
- `docs/AGENTS.md` is product documentation for Agenthedge's internal trading-agent roster. Do not treat it as Codex operating instructions.
- Follow repository documentation that is relevant to the task, especially `README.md`, `docs/TESTING.md`, `docs/SECURITY.md`, `docs/RISK_MANAGEMENT.md`, `docs/COMPLIANCE.md`, `docs/DATA_GOVERNANCE.md`, and `docs/READINESS_CHECKLIST.md`.

## Default Workflow

1. Inspect the relevant files, current branch state, and existing tests before editing.
2. Classify the task before implementation. If a `route-task` helper exists in the future, run it before making changes.
3. Keep the change narrowly scoped to the requested behavior.
4. Treat protected paths as higher risk and stop if the requested change cannot be safely contained.
5. Run the narrowest useful verification first, then broader checks when the change touches shared behavior.
6. Report exactly what changed and what verification did or did not run.

## Protected Areas

Use extra care around:

- authentication and authorization
- runtime configuration and environment variables
- server/API boundaries
- deploy, CI, and workflow configuration
- database schemas, migrations, and persistent state
- secrets, credentials, and provider keys
- trading, execution, compliance, risk, and tenant-sensitive logic

For protected work, prefer advisory analysis or a tightly bounded implementation with explicit verification. Do not broaden scope without reclassifying the task.

## Verification

Use the smallest relevant checks for the files changed. Common commands include:

```bash
poetry run pytest
poetry run pytest tests/agents/test_runtime.py -k pipeline
poetry run black src tests
poetry run mypy src
poetry build && poetry run python scripts/package_smoke.py
```

If verification requires secrets, live services, broker/provider credentials, or unavailable infrastructure, state that clearly instead of claiming success.

## Local State

- Do not modify `.env`, secrets, generated logs, or storage artifacts unless the user explicitly asks.
- Do not revert user changes. If unrelated local changes exist, leave them untouched.
- Keep commits and pull requests focused on one coherent task.
