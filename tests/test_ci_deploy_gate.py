"""Regression tests for the Railway CI pre-deploy gate."""
from __future__ import annotations

import os
from email.message import Message
from unittest import mock
from urllib.error import HTTPError

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


def test_github_token_prefers_dedicated_read_only_variable():
    with mock.patch.dict(
        os.environ,
        {"CI_DEPLOY_GATE_GITHUB_TOKEN": "dedicated", "GITHUB_TOKEN": "fallback"},
        clear=True,
    ):
        assert gate._github_token() == "dedicated"


def test_fetch_runs_adds_authorization_only_when_token_exists():
    response = mock.MagicMock()
    response.status = 200
    response.read.return_value = b'{"workflow_runs": []}'
    response.__enter__.return_value = response

    with mock.patch.dict(os.environ, {"CI_DEPLOY_GATE_GITHUB_TOKEN": "secret"}, clear=True):
        with mock.patch.object(gate, "urlopen", return_value=response) as urlopen:
            gate.fetch_runs(SHA)
            request = urlopen.call_args.args[0]
            assert request.get_header("Authorization") == "Bearer secret"

    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch.object(gate, "urlopen", return_value=response) as urlopen:
            gate.fetch_runs(SHA)
            request = urlopen.call_args.args[0]
            assert request.get_header("Authorization") is None


def test_fetch_runs_converts_exhausted_403_into_rate_limit_error():
    headers = Message()
    headers["X-RateLimit-Remaining"] = "0"
    headers["X-RateLimit-Reset"] = str(int(gate.time.time()) + 30)
    error = HTTPError("https://api.github.com", 403, "rate limited", headers, None)

    with mock.patch.object(gate, "urlopen", side_effect=error):
        try:
            gate.fetch_runs(SHA)
        except gate.RateLimitError as exc:
            assert exc.retry_after is not None
            assert exc.retry_after >= 1
        else:
            raise AssertionError("rate-limited 403 did not raise RateLimitError")


def test_wait_for_quality_check_rate_limit_remains_fail_closed():
    with mock.patch.object(
        gate,
        "fetch_runs",
        side_effect=gate.RateLimitError("limited", retry_after=1),
    ):
        with mock.patch.object(gate.time, "sleep"):
            times = iter([0.0, 0.0, 2.0, 2.0])
            with mock.patch.object(gate.time, "monotonic", side_effect=lambda: next(times)):
                ok, detail = gate.wait_for_quality_check(
                    SHA,
                    timeout_seconds=1,
                    poll_seconds=1,
                )
    assert ok is False
    assert "timeout waiting for Quality Check" in detail
