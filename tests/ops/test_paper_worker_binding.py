"""Construction must read an explicit mandate installation, never create one."""

import hashlib
import json
from dataclasses import asdict, replace

import pytest

from ops.worker_builder import build_worker
from portfolio.paper_mandate import PaperMandate
from tests.ops.test_release_gate import dossier, sign
from tests.ops.test_worker_builder import construction  # noqa: F401


def bind_candidate(prepared):
    args, journal, identity = prepared
    mandate = PaperMandate.from_mapping(
        dict(
            account_id=identity.account_id,
            allocation="10000",
            max_order_shares=1,
            max_order_notional="1000",
            max_position_shares=1,
            max_position_notional="1000",
            max_instrument_fraction=".1",
            max_sector_fraction=".25",
            max_gross_fraction=".1",
            max_outstanding_orders=1,
            symbol="SPY",
            strategy="momentum",
        )
    )
    document = json.loads(args["strategy_path"].read_text())
    document["paper_mandate"] = asdict(mandate)
    args["strategy_path"].write_text(json.dumps(document, default=str))
    identity = replace(
        identity, strategy_hash=hashlib.sha256(args["strategy_path"].read_bytes()).hexdigest()
    )
    trust = json.loads(args["trust_path"].read_text())
    trust["identity"] = asdict(identity)
    args["trust_path"].write_text(json.dumps(trust))
    args["evidence_path"].write_text(json.dumps(sign(dossier(identity))))
    return args, journal, identity, mandate


def test_construction_rejects_missing_explicit_mandate_installation(construction):  # noqa: F811
    args, _, _, _ = bind_candidate(construction)
    with pytest.raises(ValueError, match="installed paper mandate"):
        worker = build_worker(**args)
        worker.runtime.stop()


def test_constructed_session_observer_binds_installed_mandate(construction):  # noqa: F811
    args, journal, identity, mandate = bind_candidate(construction)
    journal.install_paper_mandate(identity.account_id, identity.mode, mandate)
    worker = build_worker(**args)
    try:
        assert worker.runtime._agent_extras["session_risk"].paper_mandate == mandate
        assert worker.runtime._tick_count == 0
    finally:
        worker.runtime.stop()
