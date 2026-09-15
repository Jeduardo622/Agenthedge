"""Synthetic signatures for isolated admission tests; no deployed trust."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ops.release_gate import ReleaseTrust
from ops.runtime_release import RuntimeReleaseAuthorization
from tests.ops.test_release_gate import IDENTITY, KEY, digest, dossier, sign


def paper_release(config, account, now, *, expires_in=timedelta(hours=1)):
    if not isinstance(now, datetime) or now.utcoffset() is None:
        now = datetime(2026, 9, 14, 21, tzinfo=timezone.utc)
    identity = replace(
        IDENTITY, account_id=account, mode="paper_broker", config_hash=config.release_config_hash()
    )
    payload = dossier(identity)
    payload["issued_at"] = now.isoformat()
    payload["expires_at"] = (now + expires_in).isoformat()
    replacements = {}
    artifacts = {}
    for old, artifact in payload["artifacts"].items():
        artifact["observed_at"] = now.isoformat()
        new = digest(artifact)
        artifacts[new] = artifact
        replacements[old] = new
    payload["artifacts"] = artifacts
    for checks in payload["gates"].values():
        for name, old in checks.items():
            checks[name] = replacements[old]
    payload["sessions"] = []
    trust = ReleaseTrust(identity, {"test-reviewer": KEY}, "paper-owner")
    evidence = sign(payload)
    return trust, evidence, RuntimeReleaseAuthorization.build(config, account, trust, evidence)
