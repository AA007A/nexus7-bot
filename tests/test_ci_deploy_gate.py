"""Regression tests for the Railway CI pre-deploy gate."""
from __future__ import annotations

import os
from unittest import mock

from bot import ci_deploy_gate as gate

SHA = "a" * 40


def _payload(*, status="completed", conclusion="success", event="push", name="Quality Check", sha=SHA):
    return {
        "workflow_runs": [
            {
                "head_sha": sha,
                "name": name,
                "event": event,
                "status": status,
                "conclusion": conclusion,
                "created_at": "2026-09-06T15:00:00Z",
            }
        ]
    }


def test_validate_sha_rejects_missing_and_invalid_values():
    for value in (None, "", "abc", "g" * 40, "a" * 39):
        try:
            gate.validate_sha(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid SHA accepted: {value!r}")


def test_classify_runs_requires_exact_successful_push_check():
    assert gate.classify_runs(_payload(), SHA)[0] == "success"
    assert gate.classify_runs(_payload(status="in_progress", conclusion=None), SHA)[0] == "pending"
    assert gate.classify_runs(_payload(conclusion="failure"), SHA)[0] == "failed"
    assert gate.classify_runs(_payload(event="pull_request"), SHA)[0] == "missing"
    assert gate.classify_runs(_payload(name="Other"), SHA)[0] == "missing"
    assert gate.classify_runs(_payload(sha="b" * 40), SHA)[0] == "missing"


def test_main_fails_closed_without_railway_commit_sha():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert gate.main() != 0


def test_main_passes_only_after_exact_quality_check_success():
    with mock.patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": SHA}, clear=True):
        with mock.patch.object(gate, "wait_for_quality_check", return_value=(True, "ok")) as wait:
            assert gate.main() == 0
            wait.assert_called_once()


def test_main_blocks_when_quality_check_fails():
    with mock.patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": SHA}, clear=True):
        with mock.patch.object(gate, "wait_for_quality_check", return_value=(False, "failed")):
            assert gate.main() != 0
