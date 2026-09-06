from __future__ import annotations

import os
import unittest
from unittest import mock

from bot import validation_safety_lock as gate


class _Log:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def critical(self, *args, **kwargs):
        pass


class QualityStateTests(unittest.TestCase):
    def test_success_requires_exact_push_quality_check_and_sha(self):
        sha = "abc123"
        payload = {
            "workflow_runs": [
                {"id": 1, "name": "Quality Check", "event": "pull_request",
                 "head_sha": sha, "status": "completed", "conclusion": "success"},
                {"id": 2, "name": "Quality Check", "event": "push",
                 "head_sha": "other", "status": "completed", "conclusion": "success"},
                {"id": 3, "name": "Quality Check", "event": "push",
                 "head_sha": sha, "status": "completed", "conclusion": "success"},
            ]
        }
        state, detail = gate._quality_state(payload, sha)
        self.assertEqual(state, "success")
        self.assertIn("run=3", detail)

    def test_failed_or_cancelled_quality_check_blocks(self):
        sha = "abc123"
        payload = {"workflow_runs": [{
            "id": 4, "name": "Quality Check", "event": "push",
            "head_sha": sha, "status": "completed", "conclusion": "failure",
        }]}
        state, _ = gate._quality_state(payload, sha)
        self.assertEqual(state, "failed")

    def test_missing_run_is_not_success(self):
        state, _ = gate._quality_state({"workflow_runs": []}, "abc123")
        self.assertEqual(state, "missing")

    def test_invalid_schema_fails_closed(self):
        state, _ = gate._quality_state({"workflow_runs": None}, "abc123")
        self.assertEqual(state, "failed")


class ReleaseGateAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_railway_runtime_does_not_require_network(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(gate, "_fetch_quality_payload") as fetch:
                self.assertTrue(await gate._wait_for_quality_gate(_Log()))
                fetch.assert_not_called()

    async def test_production_success_allows_engine_start(self):
        sha = "abc123"
        payload = {"workflow_runs": [{
            "id": 5, "name": "Quality Check", "event": "push",
            "head_sha": sha, "status": "completed", "conclusion": "success",
        }]}
        env = {"RAILWAY_GIT_COMMIT_SHA": sha, "RAILWAY_ENVIRONMENT_NAME": "production"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(gate, "_fetch_quality_payload", return_value=payload):
                self.assertTrue(await gate._wait_for_quality_gate(_Log()))

    async def test_production_failed_quality_check_blocks(self):
        sha = "abc123"
        payload = {"workflow_runs": [{
            "id": 6, "name": "Quality Check", "event": "push",
            "head_sha": sha, "status": "completed", "conclusion": "cancelled",
        }]}
        env = {"RAILWAY_GIT_COMMIT_SHA": sha, "RAILWAY_ENVIRONMENT_NAME": "production"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(gate, "_fetch_quality_payload", return_value=payload):
                self.assertFalse(await gate._wait_for_quality_gate(_Log()))


if __name__ == "__main__":
    unittest.main()
