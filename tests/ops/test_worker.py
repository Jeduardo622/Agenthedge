"""Durable worker does not execute anything merely because it is constructed."""

from ops.worker import DurableWorker, InstalledArtifacts, PaperTarget


def test_worker_contract_is_concrete():
    assert callable(DurableWorker.run_once)
    assert callable(InstalledArtifacts.require)
    assert PaperTarget.__dataclass_params__.frozen
