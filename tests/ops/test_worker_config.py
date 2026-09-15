"""Controller trust is configured independently of candidate release evidence."""

import json
from dataclasses import asdict

import pytest

from ops.worker_config import load_worker_authority, parse_session_controls
from tests.ops.test_release_gate import IDENTITY, KEY, NOW, dossier, sign


@pytest.fixture
def files(tmp_path):
    trust = tmp_path / "owner-trust.json"
    evidence = tmp_path / "candidate-evidence.json"
    trust.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": asdict(IDENTITY),
                "issuer_key_environment": {"test-reviewer": "TEST_SIGNING_KEY"},
                "paper_account_id": "paper-owner",
            }
        )
    )
    evidence.write_text(json.dumps(sign(dossier())))
    return trust, evidence


def test_explicit_owner_identity_and_named_secret_are_independent_of_candidate(files):
    trust_path, evidence_path = files
    authority = load_worker_authority(
        trust_path, evidence_path, environment={"TEST_SIGNING_KEY": KEY.decode()}
    )
    assert authority.trust.expected == IDENTITY
    assert authority.check(now=NOW)["passed"]
    assert KEY.decode() not in repr(authority)
    assert KEY.decode() not in trust_path.read_text()


def test_missing_secret_never_loads_dotenv_or_accepts_candidate_as_trust(files, monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: pytest.fail("dotenv read"))
    trust, evidence = files
    with pytest.raises(ValueError, match="issuer key unavailable") as error:
        load_worker_authority(trust, evidence, environment={})
    assert KEY.decode() not in str(error.value)
    with pytest.raises(ValueError, match="distinct"):
        load_worker_authority(evidence, evidence, environment={"TEST_SIGNING_KEY": KEY.decode()})


def test_candidate_identity_and_signature_cannot_supply_authority(files):
    trust, evidence = files
    payload = dossier()
    payload["identity"]["account_id"] = "candidate-other-account"
    evidence.write_text(json.dumps(sign(payload)))
    authority = load_worker_authority(
        trust, evidence, environment={"TEST_SIGNING_KEY": KEY.decode()}
    )
    assert not authority.check(now=NOW)["passed"]
    assert authority.trust.expected.account_id == IDENTITY.account_id


def test_expired_evidence_can_be_loaded_for_recovery_but_never_authorizes_start(files):
    from datetime import timedelta

    authority = load_worker_authority(*files, environment={"TEST_SIGNING_KEY": KEY.decode()})
    assert not authority.check(now=NOW + timedelta(days=1))["passed"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"issuer_key_environment": {"test-reviewer": "bad env name"}},
        {"issuer_key_environment": {}},
        {"schema_version": True},
        {"trusted_keys": {"test-reviewer": "secret-inline"}},
    ],
)
def test_malformed_trust_configuration_fails_closed(files, mutation):
    trust, evidence = files
    document = json.loads(trust.read_text())
    document.update(mutation)
    trust.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_worker_authority(trust, evidence, environment={"TEST_SIGNING_KEY": KEY.decode()})


def test_explicit_session_controls_have_no_fallback_or_nonfinite_values():
    from datetime import timedelta
    from decimal import Decimal

    document = {
        "max_mark_age_seconds": 30,
        "boundary_grace_seconds": 2700,
        "window_sessions": 30,
        "max_drawdown": "0.10",
        "control_timeout_seconds": 30,
    }
    value = parse_session_controls(document)
    assert value.max_mark_age == timedelta(seconds=30)
    assert value.max_drawdown == Decimal("0.10")
    for field, bad in [
        ("max_mark_age_seconds", "NaN"),
        ("boundary_grace_seconds", -1),
        ("window_sessions", True),
        ("max_drawdown", "Infinity"),
    ]:
        with pytest.raises(ValueError):
            parse_session_controls({**document, field: bad})
    with pytest.raises(ValueError):
        parse_session_controls({})
