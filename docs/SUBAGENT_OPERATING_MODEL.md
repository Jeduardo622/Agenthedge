# Subagent Operating Model

This document defines how Agenthedge executes software delivery autonomously using specialized subagents.

## Goals

- Enable end-to-end autonomous engineering work with explicit gates.
- Keep changes safe by aligning implementation work to existing runtime/governance patterns.
- Minimize human intervention to approval moments (scope, risk, release).

## Delivery Pipeline (Mandatory)

Every autonomous task must pass through these stages in order:

1. **Requirements**
2. **Research**
3. **Specification**
4. **Architecture**
5. **Implementation**
6. **Review**
7. **Testing**
8. **Iteration**

No subagent may skip a stage unless the task is explicitly marked trivial by the Orchestrator.

## Subagent Roles

### 1) Orchestrator Agent

Owns task intake, sequencing, and quality gates.

**Responsibilities**
- Convert user request into a bounded task contract.
- Assign subagents by stage.
- Enforce stage completion criteria.
- Aggregate final report and release recommendation.

**Inputs**
- User intent, repository state, policy docs.

**Outputs**
- Task contract, execution plan, stage approvals, final handoff summary.

### 2) Requirements Agent

Defines objective, constraints, and acceptance criteria.

**Responsibilities**
- Convert request into precise functional/non-functional requirements.
- Record constraints (security, risk, compatibility, performance).
- Define acceptance checks before implementation starts.

**Deliverable Template**
- Objective
- In-scope / out-of-scope
- Constraints
- Acceptance criteria
- Risks + unknowns

### 3) Research Agent

Grounds decisions in local repository evidence and official docs.

**Responsibilities**
- Inspect relevant source files/tests/docs.
- Verify third-party behaviors from primary documentation.
- Produce evidence-backed findings (no invented APIs).

**Deliverable Template**
- Verified facts
- Unknowns and assumptions
- Source map (`file_path`, docs links)

### 4) Specification Agent

Produces concrete change spec with edge cases.

**Responsibilities**
- Define exact behavior and interfaces.
- Declare migration, rollout, and rollback expectations.
- Map requirements to code-level acceptance tests.

**Deliverable Template**
- Feature behavior matrix
- Interface changes
- Error handling and edge cases
- Test plan matrix

### 5) Architecture Agent

Designs component interactions and boundaries.

**Responsibilities**
- Choose the simplest architecture that meets requirements.
- Reuse existing modules first.
- Propose extension points and dependency impacts.

**Deliverable Template**
- Component diagram (textual)
- Data/control flow
- Integration points
- Alternatives considered

### 6) Implementation Agent

Executes minimal, production-grade changes.

**Responsibilities**
- Implement only approved spec/architecture.
- Follow repository style and conventions.
- Keep patches small and reviewable.

**Deliverable Template**
- Change summary by file
- Notable implementation decisions
- Follow-up TODOs (if any)

### 7) Review Agent

Performs critical static and semantic review.

**Responsibilities**
- Detect correctness, reliability, and maintainability issues.
- Verify error paths and policy compliance.
- Suggest concrete fixes.

**Deliverable Template**
- Findings by severity
- Suggested diffs
- Decision: approve/request changes

### 8) Testing Agent

Runs test strategy and validates confidence.

**Responsibilities**
- Execute targeted + impacted tests first, then broader suites as needed.
- Verify deterministic behavior where possible.
- Capture failures with reproducible commands.

**Deliverable Template**
- Test matrix
- Command outputs summary
- Coverage and confidence statement

### 9) Iteration Agent

Closes gaps from review/testing until release quality is met.

**Responsibilities**
- Apply minimal corrections.
- Re-run focused validation.
- Produce final readiness checklist.

## Stage Gates

A stage is considered complete only if all conditions are true:

- Deliverable template is fully filled.
- Evidence includes repository references.
- Open questions are either resolved or explicitly escalated.
- Next stage has a clear entry checklist.

If a gate fails, control returns to the owning subagent for revision.

## Escalation Rules

Escalate to a human before implementation when any of the following occurs:

- Ambiguous requirements affecting behavior or legal/risk posture.
- Security-sensitive changes (credentials, auth, network policy).
- External dependency additions with unclear licensing/support.
- Schema/state migration without rollback clarity.

## Default Definition of Done

A task is done when:

- Requirements acceptance criteria are satisfied.
- Code review has no unresolved high-severity findings.
- Relevant tests pass.
- Changelog/docs are updated when behavior changes.
- Release risk is explicitly stated.

## Mapping to Agenthedge Codebase

- Runtime and orchestration: `src/agents/runtime.py`, `src/agents/runtime_builder.py`
- Built-in agent behaviors: `src/agents/impl/*.py`
- Messaging interfaces: `src/agents/messaging.py`
- Operational controls: `src/cli/runtime.py`
- Governance/readiness references: `docs/ROADMAP.md`, `docs/READINESS_CHECKLIST.md`

## Recommended First Autonomous Work Queue

1. Validate approval-chain invariants and replay protection in CI.
2. Validate runtime kill-switch + execution fill-block behavior in integration tests.
3. Enforce and test bus ACLs for non-development profiles.
4. Enforce and test network allowlist for provider/webhook egress.
5. Automate audit-chain verification and release artifact checks.
