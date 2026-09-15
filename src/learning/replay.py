"""Read-only, repeatable journal export with original intent ownership."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from infra.postgres import postgres_connection
from learning.attribution import attribute_economic_envelopes
from portfolio.journal import PostgresJournal


def export_journal_attribution(
    journal: PostgresJournal, *, account_id: str, mode: str
) -> dict[str, Any]:
    """Rebuild an attribution report without changing the journal or active weights.

    A repeatable, read-only transaction binds economic events and original intent
    ownership to the same snapshot. Missing ownership remains unavailable; correction
    replacements retain the original entry's owners in the canonical reducer.
    """
    if (
        not isinstance(account_id, str)
        or not account_id
        or account_id != account_id.strip()
        or mode not in {"simulated", "paper_broker", "live"}
    ):
        raise ValueError("explicit canonical attribution namespace required")
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute(
            "SELECT checkpoint,genesis FROM ah_execution_accounts WHERE account_id=%s AND mode=%s",
            (account_id, mode),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("attribution namespace does not exist")
        checkpoint = row[0]
        if type(checkpoint) is not int or checkpoint < 0:
            raise ValueError("invalid economic journal checkpoint")
        genesis = row[1]
        if not isinstance(genesis, dict) or genesis.get("positions") != {}:
            raise ValueError(
                "initial position ownership unavailable for complete attribution replay"
            )
        cur.execute(
            "SELECT o.state->>'broker_order_id',i.client_order_id,i.payload "
            "FROM ah_execution_orders o JOIN ah_execution_intents i "
            "USING(account_id,mode,client_order_id) WHERE o.account_id=%s AND o.mode=%s "
            "AND o.state->>'broker_order_id' IS NOT NULL",
            (account_id, mode),
        )
        ownership = {}
        for broker_id, client, payload in cur.fetchall():
            if broker_id in ownership or not isinstance(payload, dict):
                raise ValueError("ambiguous or invalid durable intent ownership")
            ownership[broker_id] = (client, payload)
        cur.execute(
            "SELECT sequence,event FROM ah_execution_events "
            "WHERE account_id=%s AND mode=%s AND sequence<=%s ORDER BY sequence",
            (account_id, mode, checkpoint),
        )
        events = cur.fetchall()
    if len(events) != checkpoint:
        raise ValueError("incomplete economic journal attribution snapshot")
    envelopes = []
    for expected_sequence, (sequence, event) in enumerate(events, 1):
        if (
            sequence != expected_sequence
            or not isinstance(event, dict)
            or event.get("account_id") != account_id
            or event.get("mode") != mode
        ):
            raise ValueError("invalid economic journal attribution snapshot")
        envelope: dict[str, Any] = {"economic_event": event}
        payload = event["payload"]
        if payload["kind"] == "trade" and payload["order_id"] in ownership:
            client, original = ownership[payload["order_id"]]
            envelope["director_approval_id"] = client
            for key in ("strategies", "proposal_id", "decision_id"):
                if key in original:
                    envelope[key] = original[key]
        envelopes.append(envelope)
    result = attribute_economic_envelopes(envelopes)
    canonical = json.dumps(envelopes, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "schema_version": 1,
        "account_id": account_id,
        "mode": mode,
        "checkpoint": checkpoint,
        "envelopes_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "envelopes": envelopes,
        "realized_pnl": {key: str(value) for key, value in result.realized_pnl.items()},
        "unavailable_event_ids": list(result.unavailable_event_ids),
    }
