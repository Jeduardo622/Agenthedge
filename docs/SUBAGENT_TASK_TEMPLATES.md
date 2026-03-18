# Subagent Task Templates

Use these templates to keep autonomous delivery consistent and auditable.

## 1) Task Contract (Orchestrator)

```md
# Task Contract

## Request
<user request>

## Objective
<single-sentence objective>

## Scope
- In scope:
- Out of scope:

## Constraints
- Technical:
- Security/compliance:
- Timeline:

## Acceptance Criteria
- [ ]
- [ ]

## Risks
- Risk:
  - Mitigation:

## Assigned Subagents
1. Requirements Agent
2. Research Agent
3. Specification Agent
4. Architecture Agent
5. Implementation Agent
6. Review Agent
7. Testing Agent
8. Iteration Agent
```

## 2) Stage Handoff Packet

```md
# Stage Handoff: <Stage Name>

## Summary
<what was completed>

## Evidence
- File references:
- External references (if any):

## Decisions
- Decision:
  - Rationale:

## Open Questions
- Question:
  - Owner:
  - Needed by:

## Next Stage Entry Checklist
- [ ]
- [ ]
```

## 3) Final Delivery Report

```md
# Autonomous Delivery Report

## Completed Stages
- [x] Requirements
- [x] Research
- [x] Specification
- [x] Architecture
- [x] Implementation
- [x] Review
- [x] Testing
- [x] Iteration

## Change Summary
- File:
  - Change:

## Validation
- Command:
  - Result:

## Residual Risks
- Risk:
  - Impact:
  - Mitigation:

## Follow-ups
- [ ]
```

## 4) Severity Rubric for Review Agent

- **S0 (Blocker):** Security/data-loss/legal risk, release cannot proceed.
- **S1 (High):** Likely correctness or reliability failure under normal use.
- **S2 (Medium):** Maintainability or edge-case defect with limited blast radius.
- **S3 (Low):** Minor cleanup, naming, docs, or non-functional polish.
