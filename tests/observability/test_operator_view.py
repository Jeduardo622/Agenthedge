"""The operator view consumes actual durable state in an isolated account."""

from datetime import timedelta

import pytest

from infra.postgres import postgres_connection
from observability.operator_view import OperatorView
from tests.integration.test_control_commands import SHA

pytest_plugins = ["tests.integration.test_control_commands"]


def test_disconnected_view_preserves_cash_and_disables_submission(store):
    view = OperatorView(store, expected_release=SHA)
    snapshot = view.snapshot()
    assert snapshot["portfolio"]["cash"] == "1000"
    assert snapshot["portfolio"]["positions"] == {}
    assert snapshot["orders"] == [] and snapshot["economics"] == []
    assert snapshot["controls_available"] is False
    assert snapshot["worker"] is None
    with pytest.raises(ValueError, match="worker"):
        view.submit(command_id="one", action="halt", actor="synthetic-owner")
    assert store.status("one") is None


def test_current_worker_submission_refresh_and_duplicate_are_durable(store):
    store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    view = OperatorView(store, expected_release=SHA)
    assert view.snapshot()["controls_available"] is True
    for _ in range(2):
        result = view.submit(command_id="one", action="halt", actor="synthetic-owner")
        assert result["state"] == "pending" and result["applied"] is False
    refreshed = OperatorView(store, expected_release=SHA).snapshot()
    assert len(refreshed["commands"]) == 1
    assert refreshed["commands"][0]["command_id"] == "one"
    assert "authorization" not in refreshed["commands"][0]


def test_expired_and_changed_release_worker_cannot_enable_controls(store):
    store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    assert OperatorView(store, expected_release="b" * 40).snapshot()["controls_available"] is False
    with postgres_connection(store.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ah_control_workers SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE account_id=%s AND mode=%s",
            (store.account_id, store.mode),
        )
        conn.commit()
    snapshot = OperatorView(store, expected_release=SHA).snapshot()
    assert snapshot["controls_available"] is False
    assert snapshot["worker"]["lease_current"] is False
    assert snapshot["worker"]["lease_until"] < snapshot["observed_at"]


def test_other_account_and_live_namespace_never_appear(store):
    from ops.commands import CommandStore

    other = CommandStore(store.dsn, account_id="missing-account", mode="paper_broker")
    with pytest.raises(ValueError, match="account"):
        OperatorView(other, expected_release=SHA).snapshot()
    live = CommandStore(store.dsn, account_id=store.account_id, mode="live")
    with pytest.raises(ValueError, match="account"):
        OperatorView(live, expected_release=SHA).snapshot()


def test_unknown_command_and_empty_actor_write_nothing(store):
    store.acquire_worker(worker_id="worker", release=SHA, lease=timedelta(seconds=30))
    view = OperatorView(store, expected_release=SHA)
    for actor, action in (("", "halt"), ("owner", "cancel_all")):
        with pytest.raises(ValueError):
            view.submit(command_id="one", action=action, actor=actor)
    assert store.status("one") is None
