"""Regression tests for the Railway exact-SHA CI pre-deploy gate."""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from bot import ci_deploy_gate as gate

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def _response(body: bytes, status: int = 200):
    response = mock.MagicMock()
    response.status = status
    response.read.return_value = body
    response.__enter__.return_value = response
    return response


def test_validate_sha_rejects_missing_and_invalid_values():
    for value in (None, "", "abc", "g" * 40, "a" * 39):
        try:
            gate.validate_sha(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid SHA accepted: {value!r}")


def test_attestation_url_is_exact_sha_on_dedicated_branch():
    url = gate.attestation_url(SHA)
    assert url == (
        "https://raw.githubusercontent.com/AA007A/nexus7-bot/"
        f"ci-attestations/passed/{SHA}.txt"
    )


def test_fetch_attestation_passes_only_on_exact_body_and_uses_no_auth_token():
    response = _response((SHA + "\n").encode("utf-8"))
    with mock.patch.object(gate, "urlopen", return_value=response) as urlopen:
        state, detail = gate.fetch_attestation(SHA)
    request = urlopen.call_args.args[0]
    assert state == "success"
    assert "exact-SHA" in detail
    assert request.full_url == gate.attestation_url(SHA)
    assert request.get_header("Authorization") is None


def test_fetch_attestation_404_is_pending_not_success():
    error = HTTPError(gate.attestation_url(SHA), 404, "not found", None, None)
    with mock.patch.object(gate, "urlopen", side_effect=error):
        state, detail = gate.fetch_attestation(SHA)
    assert state == "pending"
    assert "not published" in detail


def test_fetch_attestation_mismatch_is_hard_failure():
    response = _response((("b" * 40) + "\n").encode("utf-8"))
    with mock.patch.object(gate, "urlopen", return_value=response):
        state, detail = gate.fetch_attestation(SHA)
    assert state == "failed"
    assert "mismatch" in detail


def test_main_fails_closed_without_railway_commit_sha():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert gate.main() != 0


def test_main_passes_only_after_exact_quality_check_attestation():
    with mock.patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": SHA}, clear=True):
        with mock.patch.object(gate, "wait_for_quality_check", return_value=(True, "ok")) as wait:
            assert gate.main() == 0
            wait.assert_called_once()


def test_main_blocks_when_attestation_wait_fails():
    with mock.patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": SHA}, clear=True):
        with mock.patch.object(gate, "wait_for_quality_check", return_value=(False, "failed")):
            assert gate.main() != 0


def test_wait_for_quality_check_missing_attestation_remains_fail_closed():
    with mock.patch.object(
        gate,
        "fetch_attestation",
        return_value=("pending", "not published"),
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
    assert "timeout waiting for Quality Check attestation" in detail


def test_wait_for_quality_check_network_failure_remains_fail_closed():
    with mock.patch.object(
        gate,
        "fetch_attestation",
        side_effect=URLError("temporary network failure"),
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
    assert "source unavailable" in detail


def test_attestation_workflow_requires_completed_successful_main_push():
    text = (ROOT / ".github" / "workflows" / "ci_attest.yml").read_text(encoding="utf-8")
    assert "workflow_run:" in text
    assert 'workflows: ["Quality Check"]' in text
    assert "types: [completed]" in text
    assert "branches: [main]" in text
    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "github.event.workflow_run.event == 'push'" in text
    assert "contents: write" in text
    assert "ci-attestations" in text
    assert 'passed/$ATTEST_SHA.txt' in text
