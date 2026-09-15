"""Streamlit view of one durable broker account and its command worker."""

from __future__ import annotations

import json
import os
from typing import cast
from uuid import uuid4

import streamlit as st

from observability.operator_view import OperatorView
from ops.commands import ACTIONS, CommandStore


def _identity() -> tuple[str, str, str, str, str]:
    values = tuple(
        os.environ.get(name, "").strip()
        for name in (
            "POSTGRES_DSN",
            "OPERATOR_ACCOUNT_ID",
            "OPERATOR_MODE",
            "OPERATOR_RELEASE",
            "OPERATOR_ACTOR",
        )
    )
    if any(not value for value in values):
        raise ValueError("durable dashboard identity is incomplete")
    dsn, account, mode, release, actor = values
    return dsn, account, mode, release, actor


def _command_id(action: str) -> str:
    ids = cast(dict[str, str], st.session_state.setdefault("command_ids", {}))
    if action not in ids:
        ids[action] = str(uuid4())
    return ids[action]


def _status_label(command: dict) -> str:
    state = command.get("state")
    if state == "succeeded" and command.get("applied"):
        return "SUCCEEDED (observed)"
    if state == "acknowledged":
        return "ACKNOWLEDGED / OUTCOME UNCERTAIN"
    if state == "pending":
        return "PENDING"
    if state == "recovery_required":
        return "RECOVERY REQUIRED / OUTCOME UNCERTAIN"
    return str(state).upper()


def _safe_error(prefix: str, exc: Exception) -> None:
    st.error(f"{prefix} ({type(exc).__name__})")


def _display_value(value: object) -> str:
    return "Unavailable" if value is None else str(value)


def _order_row(order: dict) -> dict:
    reservation = order.get("initial_reservation") or {}
    observed = order.get("observation") or {}
    return {
        "Symbol": observed.get("symbol", reservation.get("symbol", "Unavailable")),
        "Side": observed.get("side", reservation.get("side", "Unavailable")),
        "State": observed.get("status", order.get("intent_status", "Unavailable")),
        "Remaining": _display_value(order.get("remaining_quantity")),
        "Reserved cash": _display_value(order.get("current_reserved_buying_power")),
        "Filled": _display_value(order.get("posted_quantity")),
        "Fill value": _display_value(order.get("posted_value")),
        "Fees": _display_value(order.get("posted_fee")),
        "Economic gap": order.get("economic_gap"),
        "Client order": order.get("client_order_id"),
        "Broker order": _display_value(order.get("broker_order_id")),
    }


def render(view: OperatorView, *, actor: str) -> None:
    st.title("Agenthedge durable operator view")
    try:
        snapshot = view.snapshot()
    except Exception as exc:
        _safe_error("Durable state unavailable", exc)
        return
    st.caption(
        f"Account {snapshot['account_id']} | Mode {snapshot['mode']} | "
        f"Release {snapshot['expected_release']} | Read {snapshot['observed_at']}"
    )
    worker = snapshot.get("worker")
    st.button("Refresh durable readback")
    if not snapshot["controls_available"]:
        st.error("Commands disabled: current matching worker lease is unavailable.")
    elif str(snapshot["risk"].get("halt_state", "")).upper() == "HALTING":
        st.warning("HALTING: drain and readback are still pending.")
    portfolio = snapshot["portfolio"]
    cash, realized, valuation = st.columns(3)
    cash.metric("Cash", _display_value(portfolio.get("cash")))
    realized.metric("Realized P&L", _display_value(portfolio.get("realized_pnl")))
    valuation.metric("Marked valuation", _display_value(portfolio.get("marked_value")))
    st.caption(f"Portfolio updated: {_display_value(snapshot['portfolio_updated_at'])}")
    st.write(
        "Worker lease:",
        _display_value(worker),
        "| Reconciliation:",
        _display_value(snapshot["reconciliation"]),
    )
    st.write(
        "Halt:",
        _display_value(snapshot["risk"].get("halt_state")),
        "| Reason:",
        _display_value(snapshot["risk"].get("halt_reason")),
        "| Recovery:",
        _display_value(snapshot["recovery_reason"]),
    )
    st.write("Session risk policy/state:", _display_value(snapshot["risk"].get("session")))
    positions = [
        {"symbol": symbol, **position}
        for symbol, position in portfolio.get("positions", {}).items()
    ]
    st.subheader("Positions")
    if not positions:
        st.info("No positions")
    else:
        st.dataframe(positions, hide_index=True)
    st.subheader("Orders and reservations")
    if not snapshot["orders"]:
        st.info("No orders or reservations")
    else:
        st.dataframe([_order_row(order) for order in snapshot["orders"]], hide_index=True)
    st.subheader("Fills and economic activity")
    if not snapshot["economics"]:
        st.info("No trades or economic activity")
    else:
        st.dataframe(snapshot["economics"], hide_index=True)
    actions = sorted(
        action
        for action in ACTIONS
        if not (action == "start_paper" and snapshot["mode"] != "paper_broker")
        and not (
            action in {"request_live_start", "rollback_to_paper"} and snapshot["mode"] != "live"
        )
    )
    action = st.selectbox("Durable request", actions)
    command_id = _command_id(action)
    st.code(command_id, language=None)
    confirmed = True
    if snapshot["mode"] == "live" and action == "request_live_start":
        phrase = f"{snapshot['account_id']} live {snapshot['expected_release']}"
        confirmed = st.text_input(f"Type exact live identity: {phrase}") == phrase
    if st.button(
        "Submit durable request",
        disabled=not snapshot["controls_available"] or not confirmed,
    ):
        try:
            view.submit(command_id=command_id, action=action, actor=actor)
            snapshot = view.snapshot()
        except Exception as exc:
            _safe_error("Request or durable readback failed", exc)
    if st.button("New request identity"):
        st.session_state["command_ids"][action] = str(uuid4())
        st.rerun()
    last = next(
        (command for command in snapshot["commands"] if command["command_id"] == command_id),
        None,
    )
    if last:
        st.info(_status_label(last))
    st.subheader("Durable command history")
    if not snapshot["commands"]:
        st.info("No durable command requests")
    else:
        st.dataframe(
            [
                {
                    "command_id": command["command_id"],
                    "action": command["action"],
                    "status": _status_label(command),
                    "requested_at": command["requested_at"],
                    "acknowledged_at": command["acknowledged_at"],
                    "observed_at": command["observed_at"],
                }
                for command in snapshot["commands"]
            ],
            hide_index=True,
        )
    st.subheader("Strategy and approval decisions")
    if not snapshot["decisions"]:
        st.info("No bounded decisions")
    else:
        st.dataframe(snapshot["decisions"], hide_index=True)
    with st.expander("Raw bounded durable snapshot"):
        st.json(snapshot, expanded=False)
    encoded = json.dumps(snapshot, sort_keys=True, indent=2, allow_nan=False)
    st.download_button(
        "Export bounded JSON snapshot",
        encoded,
        file_name=f"operator-{snapshot['account_id']}-{snapshot['mode']}.json",
        mime="application/json",
    )


def main() -> None:
    dsn, account, mode, release, actor = _identity()
    render(
        OperatorView(CommandStore(dsn, account_id=account, mode=mode), expected_release=release),
        actor=actor,
    )


if __name__ == "__main__":
    main()
