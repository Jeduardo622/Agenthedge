"""A fault-drill report cannot turn missing, skipped or failed execution into success."""

import pytest
from scripts.qualify_runtime import RESTORE_DRILL, assess_junit

NAME = "test_process_kill_after_http_acceptance_recovers_without_repost"


@pytest.mark.parametrize(
    "returncode,body,status",
    [
        (0, "", "passed"),
        (1, "", "failed"),
        (0, "<skipped/>", "blocked"),
        (0, "<failure/>", "failed"),
        (0, "<error/>", "failed"),
    ],
)
def test_actual_test_result_controls_drill_status(returncode, body, status):
    xml = (
        f'<testsuites><testsuite><testcase name="{NAME}" time="1.5">'
        f"{body}</testcase></testsuite></testsuites>"
    )
    result = assess_junit(xml, returncode=returncode)
    assert result["status"] == status
    assert result["platform_qualified"] is False


@pytest.mark.parametrize(
    "xml",
    [
        "broken",
        "<testsuites/>",
        '<testsuite><testcase name="other"/></testsuite>',
        f'<testsuite><testcase name="{NAME}" time="NaN"/></testsuite>',
    ],
)
def test_missing_malformed_or_unexpected_evidence_fails(xml):
    assert assess_junit(xml, returncode=0)["status"] == "failed"


def test_restore_result_requires_exact_maintained_test_identity():
    xml = f'<testsuite><testcase name="{RESTORE_DRILL}" time="2.25"/></testsuite>'
    result = assess_junit(
        xml,
        returncode=0,
        expected_test=RESTORE_DRILL,
        scope="journal_transaction_and_restore",
    )
    assert result["status"] == "passed"
    assert result["scope"] == "journal_transaction_and_restore"
    assert result["platform_qualified"] is False


@pytest.mark.parametrize(
    "source,target",
    [
        (
            "postgresql://user@remote/qualification_source",
            "postgresql://user@localhost/qualification_restore",
        ),
        (
            "postgresql://user@localhost/existing",
            "postgresql://user@localhost/qualification_restore",
        ),
        (
            "postgresql://user@localhost/qualification_same",
            "postgresql://user@127.0.0.1/qualification_same",
        ),
    ],
)
def test_standalone_restore_rejects_unqualified_targets_before_connect(monkeypatch, source, target):
    import psycopg
    from scripts.qualify_runtime import exclusive_restore_databases

    def unexpected_connect(*args, **kwargs):
        raise AssertionError("target validation must precede connection")

    monkeypatch.setattr(psycopg, "connect", unexpected_connect)
    with pytest.raises(ValueError, match="disposable|distinct"):
        with exclusive_restore_databases(source, target):
            pytest.fail("invalid target admitted")
