"""Stage-C LIVE release evidence (BGX_LIVE_RELEASE_EVIDENCE_V1).

1. A locally fabricated policy payload with a correct sha256 does not prove
   LIVE provenance.
2. Evidence from deployment A cannot authorize deployment B.
3. Evidence from candidate SHA A cannot authorize SHA B.
4. Stale evidence is rejected.
5. Wrong environment/service is rejected.
6. protection_readiness="PASS" alone cannot satisfy Stage C.
7. An arbitrary non-empty human authorization cannot satisfy Stage C.
8. A candidate SHA from a CLI argument cannot establish runtime identity.
9. Research promotion is independent of LIVE provenance.
10. No Stage-C validation action mutates Railway or sends exchange orders.
"""
import copy
import datetime as dt
import inspect
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from bot import live_release_evidence as lre
from bot import nexus_oos_promotion_gate as gate
from bot import nexus_oos_replay_manifest as rm
from bot import policy_attestation as pa
from tests.test_nexus_oos_promotion_gate import _passing_artifact

NOW = dt.datetime(2026, 9, 24, 12, 0, 0, tzinfo=dt.timezone.utc)
SHA = "c" * 40
OTHER_SHA = "d" * 40
SVC, ENV = "svc-prod-pinned", "env-prod-pinned"


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kw):
    return iso(NOW - dt.timedelta(**kw))


def runtime_line(*, sha=SHA, dep="dep-A", svc=SVC, env=ENV, at=None, values=None):
    values = values if values is not None else pa.manifest_values(rm.load())
    ident = pa.runtime_identity({"RAILWAY_GIT_COMMIT_SHA": sha, "RAILWAY_DEPLOYMENT_ID": dep,
                                 "RAILWAY_SERVICE_ID": svc, "RAILWAY_ENVIRONMENT_ID": env},
                                now=NOW - dt.timedelta(hours=1) if at is None else at)
    return pa.observation_line(values, ident)


class FakeSources:
    """In-memory READ-ONLY provider; records every call for test 10."""

    source_kind = "READ_ONLY_CONTROL_PLANE"

    def __init__(self):
        self.calls = []
        self.pinned = {"service_id": SVC, "environment_id": ENV}
        self.deployments = {
            "dep-A": {"id": "dep-A", "service_id": SVC, "environment_id": ENV,
                      "commit_sha": SHA, "status": "SUCCESS", "created_at": ago(hours=2)},
            "dep-B": {"id": "dep-B", "service_id": SVC, "environment_id": ENV,
                      "commit_sha": OTHER_SHA, "status": "SUCCESS", "created_at": ago(hours=2)},
        }
        self.logs = {"dep-A": ["2026-09-24 11:00 WARNING " + runtime_line()], "dep-B": []}
        self.runs = {}
        for i, name in enumerate(lre.REQUIRED_CI_WORKFLOWS + lre.REQUIRED_PROTECTION_CHECKS):
            self.runs[str(1000 + i)] = {"id": str(1000 + i), "name": name, "head_sha": SHA,
                                        "conclusion": "success", "completed_at": ago(days=1)}
        self.approvals = {"env-approval-77": {
            "approval_reference": "env-approval-77", "candidate_sha": SHA,
            "approver_reference": "github:release-approver", "kind": "GITHUB_PROTECTED_ENVIRONMENT_APPROVAL",
            "approved_at_utc": ago(hours=1), "state": "APPROVED"}}

    def _rec(self, name, *args):
        self.calls.append(name)

    def pinned_production(self):
        self._rec("pinned_production")
        return dict(self.pinned)

    def railway_deployment(self, deployment_id):
        self._rec("railway_deployment")
        return copy.deepcopy(self.deployments.get(deployment_id, {}))

    def railway_deployment_logs(self, deployment_id):
        self._rec("railway_deployment_logs")
        return list(self.logs.get(deployment_id, []))

    def ci_run(self, run_id):
        self._rec("ci_run")
        return copy.deepcopy(self.runs.get(str(run_id), {}))

    def approval(self, reference):
        self._rec("approval")
        return copy.deepcopy(self.approvals.get(reference, {}))


def envelope(sources: FakeSources, *, dep="dep-A", sha=SHA):
    line = sources.logs["dep-A"][0]
    obs = pa.parse(line)
    names = lre.REQUIRED_CI_WORKFLOWS + lre.REQUIRED_PROTECTION_CHECKS
    ids = {n: str(1000 + i) for i, n in enumerate(names)}
    prot = {"evidence_id": "prot-1", "candidate_sha": sha, "result": "PASS",
            "generated_at_utc": ago(hours=3),
            "checks": {n: {"run_id": ids[n], "result": "PASS"} for n in lre.REQUIRED_PROTECTION_CHECKS}}
    prot["evidence_sha256"] = lre.protection_digest(prot)
    d = sources.deployments[dep]
    return {
        "schema": lre.SCHEMA, "candidate_sha": sha, "generated_at_utc": ago(minutes=10),
        "evidence_version": 1,
        "railway": {"deployment_id": dep, "service_id": d["service_id"],
                    "environment_id": d["environment_id"], "deployment_commit_sha": d["commit_sha"],
                    "deployment_status": d["status"]},
        "runtime_policy": {"candidate_sha": obs["identity"]["candidate_sha"],
                           "policy_sha256": obs["sha256"],
                           "observed_at_utc": obs["identity"]["generated_at"]},
        "ci": {"exact_sha": sha, "required_run_ids": {n: ids[n] for n in lre.REQUIRED_CI_WORKFLOWS},
               "result": "PASS"},
        "protection": prot,
        "human_approval": {"approval_reference": "env-approval-77", "candidate_sha": sha,
                           "approver_reference": "github:release-approver",
                           "kind": "GITHUB_PROTECTED_ENVIRONMENT_APPROVAL",
                           "approved_at_utc": ago(hours=1)},
    }


def artifact(sha=SHA):
    a = _passing_artifact()
    a["candidate_sha"] = sha
    a["live_policy_observation"] = {"replay_policy_sha": pa.digest(pa.manifest_values(rm.load()))}
    return a


def live(ev, src, art=None):
    return gate.evaluate_live(art or artifact(), ev, sources=src, now=NOW)


class Baseline(unittest.TestCase):
    def test_fully_source_confirmed_evidence_passes(self):
        src = FakeSources()
        r = live(envelope(src), src)
        self.assertTrue(r.promote, r.blockers)
        d = r.to_dict()
        self.assertTrue(d["production_ready"])
        self.assertTrue(d["live_provenance_authenticated"])
        self.assertFalse(d["authorizes_real_trading"])
        self.assertEqual(d["stages"]["PRELIVE_EVIDENCE"], "PASS")
        self.assertEqual(d["stages"]["LIVE_RELEASE_PRECONDITIONS"], "PASS")
        self.assertEqual(d["stages"]["REAL_ORDER_ENABLEMENT"], "HUMAN_ACTION_REQUIRED")
        self.assertTrue(all(v == "PASS" for v in d["stages"]["evidence_components"].values()))


class FabricatedPayloadIsNotProvenance(unittest.TestCase):          # 1
    def test_correct_hash_without_trusted_source_blocks(self):
        src = FakeSources()
        ev = envelope(src)
        pa.parse(src.logs["dep-A"][0])          # integrity check passes: the hash is right
        for sources in (None, type("Untrusted", (), {"source_kind": "LOCAL_JSON"})()):
            r = live(ev, sources)
            self.assertFalse(r.promote)
            self.assertIn("LIVE_PROVENANCE_SOURCE_UNAVAILABLE", r.blockers)
            self.assertFalse(r.to_dict()["production_ready"])

    def test_line_absent_from_the_deployment_logs_blocks(self):
        src = FakeSources()
        ev = envelope(src)
        src.logs["dep-A"] = ["some unrelated log line"]  # the envelope still carries a valid hash
        r = live(ev, src)
        self.assertIn("RUNTIME_POLICY_OBSERVATION_NOT_FOUND_IN_DEPLOYMENT_LOGS", r.blockers)
        self.assertEqual(r.stages["evidence_components"]["LIVE_PROVENANCE_AUTHENTICATED"], "BLOCK")
        self.assertFalse(r.promote)


class DeploymentBinding(unittest.TestCase):                           # 2
    def test_deployment_a_evidence_cannot_authorize_deployment_b(self):
        src = FakeSources()
        ev = envelope(src)
        ev["railway"]["deployment_id"] = "dep-B"            # claim B, keep A's other fields
        src.logs["dep-B"] = list(src.logs["dep-A"])         # A's line copied into B's logs
        r = live(ev, src)
        self.assertFalse(r.promote)
        self.assertIn("RUNTIME_POLICY_OBSERVATION_NOT_FOUND_IN_DEPLOYMENT_LOGS", r.blockers)
        self.assertIn("RAILWAY_DEPLOYMENT_COMMIT_SHA_NOT_CONFIRMED", r.blockers)


class ShaBinding(unittest.TestCase):                                  # 3
    def test_sha_a_runtime_cannot_authorize_sha_b(self):
        src = FakeSources()
        src.logs["dep-A"] = [runtime_line(sha=OTHER_SHA)]   # runtime runs different code
        ev = envelope(src)
        r = live(ev, src)
        self.assertFalse(r.promote)
        self.assertIn("EXACT_DEPLOYMENT_SHA_NOT_PROVEN", r.blockers)

    def test_artifact_for_another_sha_is_rejected(self):
        src = FakeSources()
        r = live(envelope(src), src, art=artifact(OTHER_SHA))
        self.assertIn("ARTIFACT_CANDIDATE_SHA_MISMATCH", r.blockers)
        self.assertFalse(r.promote)


class StaleEvidence(unittest.TestCase):                               # 4
    def _with_line(self, at):
        src = FakeSources()
        src.logs["dep-A"] = [runtime_line(at=at)]
        return src

    def test_old_runtime_observation(self):
        age = dt.timedelta(seconds=lre.MAX_RUNTIME_OBSERVATION_AGE_S + 60)
        src = self._with_line(NOW - age)
        src.deployments["dep-A"]["created_at"] = iso(NOW - age - dt.timedelta(hours=1))
        self.assertIn("RUNTIME_OBSERVATION_STALE", live(envelope(src), src).blockers)

    def test_observation_before_the_deployment(self):
        src = self._with_line(NOW - dt.timedelta(hours=3))  # deployment created 2 h ago
        self.assertIn("RUNTIME_OBSERVATION_PREDATES_DEPLOYMENT", live(envelope(src), src).blockers)

    def test_old_approval_and_ci(self):
        src = FakeSources()
        src.approvals["env-approval-77"]["approved_at_utc"] = ago(days=2)
        ev = envelope(src)
        ev["human_approval"]["approved_at_utc"] = ago(days=2)
        for run in src.runs.values():
            run["completed_at"] = ago(days=8)
        r = live(ev, src)
        self.assertIn("HUMAN_APPROVAL_NOT_PROVEN", r.blockers)
        self.assertIn("CI_NOT_PROVEN_QUALITY_CHECK", r.blockers)
        self.assertIn("PROTECTION_EVIDENCE_NOT_PROVEN", r.blockers)

    def test_limits_are_predeclared_constants(self):
        self.assertEqual(lre.MAX_RUNTIME_OBSERVATION_AGE_S, 6 * 3600)
        self.assertEqual(lre.MAX_APPROVAL_AGE_S, 24 * 3600)
        self.assertEqual(lre.MAX_CI_EVIDENCE_AGE_S, 7 * 86400)


class WrongEnvironmentOrService(unittest.TestCase):                   # 5
    def test_deployment_in_wrong_environment(self):
        src = FakeSources()
        src.deployments["dep-A"]["environment_id"] = "env-staging"
        ev = envelope(src)
        r = live(ev, src)
        self.assertIn("WRONG_ENVIRONMENT", r.blockers)
        self.assertFalse(r.promote)

    def test_runtime_line_from_another_service(self):
        src = FakeSources()
        src.logs["dep-A"] = [runtime_line(svc="svc-other")]
        r = live(envelope(src), src)
        self.assertIn("WRONG_SERVICE", r.blockers)
        self.assertFalse(r.promote)


class ProtectionStringIsNotEvidence(unittest.TestCase):               # 6
    def test_bare_pass_string(self):
        src = FakeSources()
        ev = envelope(src)
        ev["protection"] = "PASS"
        r = live(ev, src)
        self.assertIn("RELEASE_EVIDENCE_MALFORMED", r.blockers)
        self.assertFalse(r.promote)
        with self.assertRaises(TypeError):
            gate.evaluate_live(artifact(), None, protection_readiness="PASS")

    def test_result_pass_without_verified_checks(self):
        src = FakeSources()
        ev = envelope(src)
        ev["protection"]["checks"] = {}
        ev["protection"]["evidence_sha256"] = lre.protection_digest(ev["protection"])
        self.assertIn("PROTECTION_EVIDENCE_NOT_PROVEN", live(ev, src).blockers)
        ev = envelope(src)
        ev["protection"]["candidate_sha"] = OTHER_SHA        # digest now stale too
        self.assertIn("PROTECTION_EVIDENCE_NOT_PROVEN", live(ev, src).blockers)
        src.runs["1003"]["head_sha"] = OTHER_SHA              # a check ran on another SHA
        self.assertIn("PROTECTION_CHECK_NOT_PROVEN_RELEASE_PROOF", live(envelope(src), src).blockers)


class HumanApprovalMustBeStructured(unittest.TestCase):               # 7
    def test_arbitrary_string(self):
        src = FakeSources()
        ev = envelope(src)
        ev["human_approval"] = "operator said yes"
        self.assertIn("RELEASE_EVIDENCE_MALFORMED", live(ev, src).blockers)
        with self.assertRaises(TypeError):
            gate.evaluate_live(artifact(), None, human_authorization="operator")

    def test_unknown_or_foreign_approval(self):
        src = FakeSources()
        ev = envelope(src)
        ev["human_approval"]["approval_reference"] = "made-up-ref"
        self.assertIn("HUMAN_APPROVAL_NOT_PROVEN", live(ev, src).blockers)
        src.approvals["env-approval-77"]["candidate_sha"] = OTHER_SHA
        self.assertIn("HUMAN_APPROVAL_NOT_PROVEN", live(envelope(src), src).blockers)
        src = FakeSources()
        src.approvals["env-approval-77"]["state"] = "PENDING"
        self.assertIn("HUMAN_APPROVAL_NOT_PROVEN", live(envelope(src), src).blockers)


class CliShaIsNotIdentity(unittest.TestCase):                         # 8
    def test_cli_candidate_sha_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            path.write_text(json.dumps(artifact()), encoding="utf-8")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                gate.main([str(path), "--gate", "live", "--candidate-sha", SHA])

    def test_cli_with_full_envelope_still_blocks(self):
        src = FakeSources()
        with tempfile.TemporaryDirectory() as tmp:
            a, e = Path(tmp) / "a.json", Path(tmp) / "e.json"
            a.write_text(json.dumps(artifact()), encoding="utf-8")
            e.write_text(json.dumps(envelope(src)), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = gate.main([str(a), "--gate", "live", "--release-evidence", str(e)])
        out = json.loads(buf.getvalue())
        self.assertNotEqual(code, gate.EXIT_PROMOTE)
        self.assertIn("LIVE_PROVENANCE_SOURCE_UNAVAILABLE", out["blockers"])
        self.assertFalse(out["production_ready"])

    def test_envelope_and_artifact_agreeing_is_not_enough(self):
        src = FakeSources()
        ev = envelope(src)                                   # envelope + artifact both say SHA
        src.deployments["dep-A"]["commit_sha"] = OTHER_SHA   # control plane disagrees
        r = live(ev, src)
        self.assertIn("RAILWAY_DEPLOYMENT_COMMIT_SHA_NOT_CONFIRMED", r.blockers)
        self.assertIn("EXACT_DEPLOYMENT_SHA_NOT_PROVEN", r.blockers)


class ResearchIndependentOfProvenance(unittest.TestCase):            # 9
    def test_research_gate_ignores_live_evidence(self):
        self.assertNotIn("evidence", inspect.signature(gate.evaluate).parameters)
        for status in (gate.POLICY_OBSERVATION_PENDING, gate.POLICY_CONTENT_MATCH):
            a = artifact()
            a["policy_parity"] = status
            r = gate.evaluate(a)
            self.assertTrue(r.promote, r.blockers)
            self.assertFalse(r.to_dict()["production_ready"])
            self.assertFalse(gate.evaluate_live(a, None).promote)


class NoMutation(unittest.TestCase):                                  # 10
    def test_only_read_methods_are_called(self):
        src = FakeSources()
        out = lre.verify(envelope(src), artifact=artifact(), sources=src, now=NOW)
        self.assertEqual(out["mutations_performed"], 0)
        self.assertTrue(set(src.calls) <= set(lre.READ_METHODS))
        self.assertTrue(all(m.startswith(("pinned_", "railway_deployment", "ci_run", "approval"))
                            for m in lre.READ_METHODS))

    def test_verifier_source_has_no_mutating_calls(self):
        src = inspect.getsource(lre) + inspect.getsource(gate.evaluate_live) + inspect.getsource(gate.main)
        for pattern in (r"create_order", r"place_order", r"\.post\(", r"\.put\(", r"\.delete\(",
                        r"redeploy\w*\(", r"set_variables", r"restart\w*\(", r"subprocess", r"requests",
                        r"httpx", r"urllib", r"aiohttp", r"socket",
                        r"^\s*(import|from)\s+\S*(kucoin|exchange|ccxt|railway)"):
            self.assertIsNone(re.search(pattern, src, re.IGNORECASE | re.MULTILINE), pattern)


if __name__ == "__main__":
    unittest.main()
