from __future__ import annotations

import json

import pytest

from audit import JsonlAuditSink, verify_jsonl_hash_chain


def test_verify_jsonl_hash_chain_passes_for_valid_log(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    sink("risk_approval", {"proposal_id": "p1"})
    sink("execution_fill", {"proposal_id": "p1"})

    ok, errors = verify_jsonl_hash_chain(path)

    assert ok is True
    assert errors == []


def test_verify_jsonl_hash_chain_detects_tampering(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    sink("risk_approval", {"proposal_id": "p1"})
    sink("execution_fill", {"proposal_id": "p1"})

    lines = path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["payload"]["proposal_id"] = "tampered"
    lines[1] = json.dumps(second)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, errors = verify_jsonl_hash_chain(path)

    assert ok is False
    assert any("hash mismatch" in error for error in errors)


def test_jsonl_audit_sink_preserves_chain_across_restarts(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    JsonlAuditSink(path)("risk_approval", {"proposal_id": "p1"})
    JsonlAuditSink(path)("execution_fill", {"proposal_id": "p1"})

    ok, errors = verify_jsonl_hash_chain(path)

    assert ok is True
    assert errors == []


def test_jsonl_audit_sink_fails_fast_on_preexisting_invalid_chain(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    sink("risk_approval", {"proposal_id": "p1"})
    sink("execution_fill", {"proposal_id": "p1"})

    lines = path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["prev_hash"] = None
    lines[1] = json.dumps(second)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cutover_audit_chain.py|migrate_audit_chain.py"):
        JsonlAuditSink(path)
