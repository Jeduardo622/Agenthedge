from __future__ import annotations

import gc
import json
import multiprocessing
import threading
import time
import weakref

import pytest

import audit.sink as audit_sink
from audit import JsonlAuditSink, verify_jsonl_hash_chain


def _append_audit_after_release(path, action, ready, release) -> None:
    sink = JsonlAuditSink(path)
    ready.set()
    if not release.wait(timeout=10):
        raise RuntimeError("timed out waiting to append audit record")
    sink(action, {"proposal_id": "p1"})


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


def test_jsonl_audit_sink_refreshes_tail_across_independent_canonical_path_writers(
    tmp_path,
) -> None:
    path = tmp_path / "audit.jsonl"
    alias_parent = tmp_path / "alias"
    alias_parent.mkdir()
    alias_path = alias_parent / ".." / path.name
    first = JsonlAuditSink(path)
    second = JsonlAuditSink(alias_path)

    first("risk_approval", {"proposal_id": "p1"})
    second("execution_fill", {"proposal_id": "p1"})

    ok, errors = verify_jsonl_hash_chain(path)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert len(records) == 2
    assert records[1]["prev_hash"] == records[0]["hash"]
    assert ok is True
    assert errors == []


def test_jsonl_audit_sink_collects_dead_path_locks_while_live_aliases_share_one(
    tmp_path,
) -> None:
    path = tmp_path / "audit.jsonl"
    alias_parent = tmp_path / "alias"
    alias_parent.mkdir()
    alias_path = alias_parent / ".." / path.name
    first = JsonlAuditSink(path)
    second = JsonlAuditSink(alias_path)

    assert first._lock is second._lock

    unique_root = tmp_path / "unique"
    unique_root.mkdir()
    transient_sinks = [
        JsonlAuditSink(unique_root / f"audit-{index}.jsonl") for index in range(2_000)
    ]
    transient_lock_refs = [weakref.ref(sink._lock) for sink in transient_sinks]

    del transient_sinks
    gc.collect()

    assert first._lock is second._lock
    assert all(lock_ref() is None for lock_ref in transient_lock_refs)


def test_jsonl_audit_sink_concurrent_construction_shares_live_path_lock(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    alias_parent = tmp_path / "alias"
    alias_parent.mkdir()
    alias_path = alias_parent / ".." / path.name
    worker_count = 24
    start = threading.Barrier(worker_count)
    results_lock = threading.Lock()
    sinks = []
    failures = []

    def construct_sink(index) -> None:
        try:
            start.wait(timeout=10)
            sink = JsonlAuditSink(path if index % 2 == 0 else alias_path)
            with results_lock:
                sinks.append(sink)
        except BaseException as exc:
            with results_lock:
                failures.append(exc)

    threads = [
        threading.Thread(target=construct_sink, args=(index,)) for index in range(worker_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert len(sinks) == worker_count
    assert len({id(sink._lock) for sink in sinks}) == 1


def test_jsonl_audit_sink_revalidates_tail_before_append(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    path.write_text('{"not":"a valid chained record"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="existing audit chain is invalid"):
        sink("risk_approval", {"proposal_id": "p1"})

    assert path.read_text(encoding="utf-8") == '{"not":"a valid chained record"}\n'


def test_jsonl_audit_sink_serializes_hash_chain_across_processes(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    context = multiprocessing.get_context("spawn")
    ready = [context.Event(), context.Event()]
    release = context.Event()
    processes = [
        context.Process(
            target=_append_audit_after_release,
            args=(str(path), action, ready_event, release),
        )
        for action, ready_event in zip(("risk_approval", "execution_fill"), ready)
    ]

    try:
        for process in processes:
            process.start()
        assert all(event.wait(timeout=10) for event in ready)
        release.set()
        for process in processes:
            process.join(timeout=10)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    ok, errors = verify_jsonl_hash_chain(path)

    assert len(records) == 2
    assert {record["event_type"] for record in records} == {
        "risk_approval",
        "execution_fill",
    }
    assert ok is True
    assert errors == []


def test_jsonl_audit_sink_serializes_hash_chain_under_concurrent_writes(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    original_hash_record = audit_sink._hash_record
    first_hash_started = threading.Event()
    release_first_hash = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def delayed_hash_record(record):
        nonlocal calls
        with calls_lock:
            calls += 1
            should_wait = calls == 1
        if should_wait:
            first_hash_started.set()
            assert release_first_hash.wait(timeout=1)
        return original_hash_record(record)

    monkeypatch.setattr(audit_sink, "_hash_record", delayed_hash_record)

    first = threading.Thread(target=sink, args=("risk_approval", {"proposal_id": "p1"}))
    second = threading.Thread(target=sink, args=("execution_fill", {"proposal_id": "p1"}))
    first.start()
    assert first_hash_started.wait(timeout=1)
    second.start()
    time.sleep(0.01)
    release_first_hash.set()
    first.join(timeout=1)
    second.join(timeout=1)
    monkeypatch.setattr(audit_sink, "_hash_record", original_hash_record)

    assert not first.is_alive()
    assert not second.is_alive()
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
