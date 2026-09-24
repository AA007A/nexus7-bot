"""Stage-C LIVE release evidence (BGX_LIVE_RELEASE_EVIDENCE_V1).

Provenance (5f5757f):
 P1. A locally fabricated policy payload with a correct sha256 does not prove provenance.
 P2. Evidence from deployment A cannot authorize deployment B.
 P3. Evidence from candidate SHA A cannot authorize SHA B.
 P4. Stale evidence is rejected.
 P5. Wrong environment/service is rejected.
 P6. protection_readiness="PASS" alone cannot satisfy Stage C.
 P7. An arbitrary non-empty human authorization cannot satisfy Stage C.
 P8. A candidate SHA from a CLI argument cannot establish runtime identity.
 P9. Research promotion is independent of LIVE provenance.
 P10. No Stage-C validation action mutates Railway or sends exchange orders.

Trust chain (this phase):
 T1. A locally modified OOS artifact with the correct candidate SHA cannot pass Stage C.
 T2. Stage C fetches the OOS artifact from the trusted CI provider.
 T3. The fetched artifact digest must match the trusted run/artifact record.
 T4. An OOS run whose replay succeeded but whose strict research gate failed cannot authorize.
 T5. An OOS run from another SHA cannot authorize the candidate.
 T6. One generic successful run cannot satisfy all protection checks.
 T7. A protection check with the wrong workflow/job identity fails.
 T8. A trusted protection certificate may cover several checks only when all are explicitly PASS.
 T9. Candidate-controlled verifier code is not the documented Stage-C root of trust.
 T10. An approval for deployment A cannot authorize deployment B (same SHA) unless the
      provider-native approval explicitly covers both.
"""
import copy
import datetime as dt
import hashlib
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

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 9, 24, 12, 0, 0, tzinfo=dt.timezone.utc)
SHA = "c" * 40
OTHER_SHA = "d" * 40
VERIFIER_SHA = "e" * 40
SVC, ENV = "svc-prod-pinned", "env-prod-pinned"
OOS_RUN, PROT_RUN, CERT_RUN = "5000", "6000", "7000"
CI_IDS = {"Quality Check": "1000", "Supply Chain Security": "1001",
          "Runtime Truth Candidate": "1002", lre.OOS_WORKFLOW: OOS_RUN}


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


def artifact(sha=SHA):
    a = _passing_artifact()
    a["candidate_sha"] = sha
    a["live_policy_observation"] = {"replay_policy_sha": pa.digest(pa.manifest_values(rm.load()))}
    return a


def blob(obj) -> bytes:
    return json.dumps(obj, sort_keys=True).encode("utf-8")


class FakeSources:
    """In-memory READ-ONLY provider (unit tests only); records every call."""

    source_kind = "READ_ONLY_CONTROL_PLANE"

    def __init__(self, art=None):
        self.calls = []
        self.pinned = {"service_id": SVC, "environment_id": ENV}
        self.verifier = {"version": lre.RELEASE_VERIFIER_VERSION, "sha": VERIFIER_SHA,
                         "pinned_sha": VERIFIER_SHA, "source_ref": "protected-main@" + VERIFIER_SHA}
        self.deployments = {
            d: {"id": d, "service_id": SVC, "environment_id": ENV, "commit_sha": SHA,
                "status": "SUCCESS", "created_at": ago(hours=2)} for d in ("dep-A", "dep-B")}
        self.logs = {"dep-A": ["2026-09-24 11:00 WARNING " + runtime_line()],
                     "dep-B": ["2026-09-24 11:00 WARNING " + runtime_line(dep="dep-B")]}
        self.runs = {rid: {"id": rid, "name": name, "head_sha": SHA, "conclusion": "success",
                           "completed_at": ago(days=1)} for name, rid in CI_IDS.items()}
        self.runs[PROT_RUN] = {"id": PROT_RUN, "name": "Protection Suites", "head_sha": SHA,
                               "conclusion": "success", "completed_at": ago(days=1)}
        self.runs[CERT_RUN] = {"id": CERT_RUN, "name": lre.PROTECTION_WORKFLOW, "head_sha": SHA,
                               "conclusion": "success", "completed_at": ago(days=1)}
        self.jobs = {f"job-{n}": {"id": f"job-{n}", "run_id": PROT_RUN, "name": n, "head_sha": SHA,
                                  "conclusion": "success"} for n in lre.REQUIRED_PROTECTION_CHECKS}
        self.artifacts = {}
        self.put_artifact(OOS_RUN, lre.OOS_ARTIFACT_NAME, blob(art if art is not None else artifact()))
        self.put_artifact(CERT_RUN, lre.PROTECTION_ARTIFACT_NAME, blob(
            {"schema": lre.PROTECTION_SCHEMA, "candidate_sha": SHA,
             "checks": {n: "PASS" for n in lre.REQUIRED_PROTECTION_CHECKS}}))
        self.approvals = {}

    def put_artifact(self, run_id, name, content: bytes):
        self.artifacts[(run_id, name)] = {"name": name, "content": content,
                                          "sha256": hashlib.sha256(content).hexdigest()}

    def _rec(self, name):
        self.calls.append(name)

    def pinned_production(self):
        self._rec("pinned_production")
        return dict(self.pinned)

    def release_verifier(self):
        self._rec("release_verifier")
        return dict(self.verifier)

    def railway_deployment(self, deployment_id):
        self._rec("railway_deployment")
        return copy.deepcopy(self.deployments.get(deployment_id, {}))

    def railway_deployment_logs(self, deployment_id):
        self._rec("railway_deployment_logs")
        return list(self.logs.get(deployment_id, []))

    def ci_run(self, run_id):
        self._rec("ci_run")
        return copy.deepcopy(self.runs.get(str(run_id), {}))

    def ci_job(self, job_id):
        self._rec("ci_job")
        return copy.deepcopy(self.jobs.get(str(job_id), {}))

    def ci_artifact(self, run_id, name):
        self._rec("ci_artifact")
        return copy.deepcopy(self.artifacts.get((str(run_id), name), {}))

    def approval(self, reference):
        self._rec("approval")
        return copy.deepcopy(self.approvals.get(reference, {}))


def seal(ev, src, *, approved_at=None, deployment_id=None, covered=None):
    """(Re)compute the approval binding and register the provider-native record."""
    ha = ev["human_approval"]
    ha["deployment_id"] = deployment_id or ev["railway"]["deployment_id"]
    ha["research_artifact_sha256"] = ev["research_artifact"]["artifact_sha256"]
    ha["approved_at_utc"] = approved_at or ago(minutes=30)
    ha["release_evidence_digest"] = lre.release_evidence_digest(ev)
    rec = {**ha, "state": "APPROVED"}
    if covered is not None:
        rec["covered_deployment_ids"] = covered
    src.approvals[ha["approval_reference"]] = rec
    return ev


def envelope(src: FakeSources, *, dep="dep-A", sha=SHA, protection="PER_JOB"):
    obs = pa.parse(src.logs[dep][0])
    d = src.deployments[dep]
    oos = src.artifacts[(OOS_RUN, lre.OOS_ARTIFACT_NAME)]
    if protection == "PER_JOB":
        prot = {"mode": "PER_JOB",
                "checks": {n: {"check": n, "run_id": PROT_RUN, "job_id": f"job-{n}", "result": "PASS"}
                           for n in lre.REQUIRED_PROTECTION_CHECKS}}
    else:
        cert = src.artifacts[(CERT_RUN, lre.PROTECTION_ARTIFACT_NAME)]
        prot = {"mode": "CERTIFICATE",
                "certificate": {"run_id": CERT_RUN, "artifact_name": lre.PROTECTION_ARTIFACT_NAME,
                                "artifact_sha256": cert["sha256"]}}
    prot.update({"evidence_id": "prot-1", "candidate_sha": sha, "result": "PASS",
                 "generated_at_utc": ago(hours=3)})
    prot["evidence_sha256"] = lre.protection_digest(prot)
    ev = {
        "schema": lre.SCHEMA, "candidate_sha": sha, "generated_at_utc": ago(minutes=40),
        "evidence_version": 1,
        "research_artifact": {"oos_run_id": OOS_RUN, "workflow_name": lre.OOS_WORKFLOW,
                              "artifact_name": lre.OOS_ARTIFACT_NAME,
                              "artifact_sha256": oos["sha256"], "candidate_sha": sha,
                              "completed_at_utc": src.runs[OOS_RUN]["completed_at"]},
        "railway": {"deployment_id": dep, "service_id": d["service_id"],
                    "environment_id": d["environment_id"], "deployment_commit_sha": d["commit_sha"],
                    "deployment_status": d["status"]},
        "runtime_policy": {"candidate_sha": obs["identity"]["candidate_sha"],
                           "policy_sha256": obs["sha256"],
                           "observed_at_utc": obs["identity"]["generated_at"]},
        "ci": {"exact_sha": sha, "required_run_ids": dict(CI_IDS), "result": "PASS"},
        "protection": prot,
        "human_approval": {"approval_reference": "env-approval-77", "candidate_sha": sha,
                           "approver_reference": "github:release-approver",
                           "kind": "GITHUB_PROTECTED_ENVIRONMENT_APPROVAL"},
    }
    return seal(ev, src)


def live(ev, src, local=None):
    return gate.evaluate_live(local, ev, sources=src, now=NOW)


class Baseline(unittest.TestCase):
    def test_fully_source_confirmed_evidence_passes(self):
        for mode in ("PER_JOB", "CERTIFICATE"):
            src = FakeSources()
            r = live(envelope(src, protection=mode), src)
            self.assertTrue(r.promote, (mode, r.blockers))
            d = r.to_dict()
            self.assertTrue(d["production_ready"])
            self.assertFalse(d["authorizes_real_trading"])
            self.assertEqual(d["stages"]["REAL_ORDER_ENABLEMENT"], "HUMAN_ACTION_REQUIRED")
            self.assertEqual(set(d["stages"]["evidence_components"]), set(lre.COMPONENTS))
            self.assertTrue(all(v == "PASS" for v in d["stages"]["evidence_components"].values()))
            self.assertEqual(d["stages"]["release_verifier_sha"], VERIFIER_SHA)
            self.assertEqual(d["stages"]["release_verifier_version"], lre.RELEASE_VERIFIER_VERSION)


# ───────────────────────── trust chain (T1–T10) ─────────────────────────

class TrustedResearchArtifact(unittest.TestCase):
    def test_t1_locally_modified_artifact_cannot_pass(self):
        src = FakeSources()
        ev = envelope(src)
        forged = artifact()
        forged["portfolio_replay"]["ending_equity"] = 99999.0            # correct SHA, edited body
        r = live(ev, src, local=forged)
        self.assertFalse(r.promote)
        self.assertIn("LOCAL_ARTIFACT_DIFFERS_FROM_TRUSTED", r.blockers)
        # The research gate never ran on the local copy: a failing trusted artifact
        # blocks even when the local copy is perfect.
        bad = artifact()
        bad["candidate_research"]["performance"]["approved"]["net_expectancy_r"] = -0.3
        src = FakeSources(art=bad)
        r = live(envelope(src), src, local=artifact())
        self.assertIn("APPROVED_EXPECTANCY_NOT_POSITIVE", r.blockers)
        self.assertEqual(r.stages["RESEARCH_PROMOTION"], "BLOCK")

    def test_t2_artifact_is_fetched_from_the_trusted_provider(self):
        src = FakeSources()
        ev = envelope(src)
        out = lre.verify(ev, local_artifact=None, sources=src, now=NOW)
        self.assertIn("ci_artifact", src.calls)
        self.assertEqual(out["trusted_artifact"], artifact())
        self.assertEqual(out["components"]["RESEARCH_ARTIFACT_AUTHENTICATED"], "PASS")
        self.assertIn("TRUSTED_RESEARCH_ARTIFACT_UNAVAILABLE", gate.evaluate_live(artifact(), ev).blockers)

    def test_t3_digest_must_match(self):
        src = FakeSources()
        ev = envelope(src)
        rec = src.artifacts[(OOS_RUN, lre.OOS_ARTIFACT_NAME)]
        rec["content"] = rec["content"].replace(b"1120.0", b"9999.0")   # bytes changed, record not
        r = live(ev, src)
        self.assertIn("RESEARCH_ARTIFACT_DIGEST_MISMATCH", r.blockers)
        self.assertEqual(r.stages["evidence_components"]["RESEARCH_ARTIFACT_AUTHENTICATED"], "BLOCK")
        src = FakeSources()
        ev = envelope(src)
        ev["research_artifact"]["artifact_sha256"] = "0" * 64            # envelope claims another digest
        self.assertIn("RESEARCH_ARTIFACT_DIGEST_MISMATCH", live(seal(ev, src), src).blockers)

    def test_t4_replay_success_with_failed_strict_gate_cannot_authorize(self):
        src = FakeSources()
        src.runs[OOS_RUN]["conclusion"] = "failure"   # replay step ok, strict research gate failed
        r = live(envelope(src), src)
        self.assertFalse(r.promote)
        self.assertIn("OOS_RUN_NOT_SUCCESSFUL_ON_CANDIDATE_SHA", r.blockers)
        self.assertIn("CI_NOT_PROVEN_NEXUS_REAL_OOS_REPLAY", r.blockers)
        self.assertIn("NEXUS Real OOS Replay", lre.REQUIRED_CI_WORKFLOWS)

    def test_t5_oos_run_from_another_sha(self):
        src = FakeSources()
        src.runs[OOS_RUN]["head_sha"] = OTHER_SHA
        self.assertIn("OOS_RUN_NOT_SUCCESSFUL_ON_CANDIDATE_SHA", live(envelope(src), src).blockers)
        src = FakeSources(art=artifact(OTHER_SHA))       # artifact content for another SHA
        self.assertIn("RESEARCH_ARTIFACT_CANDIDATE_SHA_MISMATCH", live(envelope(src), src).blockers)


class ProtectionIdentity(unittest.TestCase):
    def test_t6_one_generic_run_cannot_satisfy_all_checks(self):
        src = FakeSources()
        ev = envelope(src)
        for n in lre.REQUIRED_PROTECTION_CHECKS:            # everything points at one job
            ev["protection"]["checks"][n]["job_id"] = "job-release_proof"
        ev["protection"]["evidence_sha256"] = lre.protection_digest(ev["protection"])
        r = live(seal(ev, src), src)
        self.assertIn("PROTECTION_EVIDENCE_NOT_PROVEN", r.blockers)
        self.assertIn("PROTECTION_CHECK_NOT_PROVEN_PRELIVE_PROTECTION_FAILCLOSED", r.blockers)
        ev = envelope(src)                                  # run id without a job id
        for n in lre.REQUIRED_PROTECTION_CHECKS:
            ev["protection"]["checks"][n].pop("job_id")
        ev["protection"]["evidence_sha256"] = lre.protection_digest(ev["protection"])
        self.assertIn("PROTECTION_EVIDENCE_NOT_PROVEN", live(seal(ev, src), src).blockers)

    def test_t7_wrong_job_identity(self):
        src = FakeSources()
        src.jobs["job-kucoin_native_tpsl"]["name"] = "lint"
        self.assertIn("PROTECTION_CHECK_NOT_PROVEN_KUCOIN_NATIVE_TPSL", live(envelope(src), src).blockers)
        src = FakeSources()
        src.jobs["job-release_proof"]["run_id"] = "9999"
        self.assertIn("PROTECTION_CHECK_NOT_PROVEN_RELEASE_PROOF", live(envelope(src), src).blockers)
        src = FakeSources()
        src.runs[CERT_RUN]["name"] = "Quality Check"          # certificate from the wrong workflow
        r = live(envelope(src, protection="CERTIFICATE"), src)
        self.assertIn("PROTECTION_EVIDENCE_NOT_PROVEN", r.blockers)

    def test_t8_certificate_must_list_every_check_as_pass(self):
        for body in ({n: "PASS" for n in lre.REQUIRED_PROTECTION_CHECKS[:-1]},
                     {**{n: "PASS" for n in lre.REQUIRED_PROTECTION_CHECKS}, "release_proof": "FAIL"}):
            src = FakeSources()
            src.put_artifact(CERT_RUN, lre.PROTECTION_ARTIFACT_NAME, blob(
                {"schema": lre.PROTECTION_SCHEMA, "candidate_sha": SHA, "checks": body}))
            self.assertIn("PROTECTION_EVIDENCE_NOT_PROVEN",
                          live(envelope(src, protection="CERTIFICATE"), src).blockers)
        src = FakeSources()
        src.put_artifact(CERT_RUN, lre.PROTECTION_ARTIFACT_NAME, blob(
            {"schema": lre.PROTECTION_SCHEMA, "candidate_sha": OTHER_SHA,
             "checks": {n: "PASS" for n in lre.REQUIRED_PROTECTION_CHECKS}}))
        self.assertIn("PROTECTION_EVIDENCE_NOT_PROVEN",
                      live(envelope(src, protection="CERTIFICATE"), src).blockers)


class VerifierTrustBoundary(unittest.TestCase):
    def test_t9_candidate_code_is_not_the_root_of_trust(self):
        src = FakeSources()
        src.verifier = {"version": "x", "sha": SHA, "pinned_sha": SHA}   # verifier == candidate
        r = live(envelope(src), src)
        self.assertIn("RELEASE_VERIFIER_NOT_TRUSTED", r.blockers)
        self.assertFalse(r.to_dict()["production_ready"])
        src = FakeSources()
        src.verifier = {"version": "x", "sha": VERIFIER_SHA, "pinned_sha": "f" * 40}  # not pinned
        self.assertIn("RELEASE_VERIFIER_NOT_TRUSTED", live(envelope(src), src).blockers)
        doc = (ROOT / "RELEASE_EVIDENCE.md").read_text(encoding="utf-8")
        for phrase in ("root of trust", "protected", "never from the candidate",
                       "not from the `source_kind` field"):
            self.assertIn(phrase, doc)
        self.assertIn("NOT the Stage-C root of trust", lre.__doc__)


class ApprovalBindsReleaseInstance(unittest.TestCase):
    def test_t10_approval_for_deployment_a_cannot_authorize_b(self):
        src = FakeSources()
        ev_a = envelope(src, dep="dep-A")
        ref = ev_a["human_approval"]["approval_reference"]
        rec_a = copy.deepcopy(src.approvals[ref])
        ev_b = envelope(src, dep="dep-B")              # same SHA, new deployment
        ev_b["human_approval"] = copy.deepcopy(ev_a["human_approval"])
        src.approvals[ref] = rec_a                     # provider only knows the approval for A
        r = live(ev_b, src)
        self.assertIn("HUMAN_APPROVAL_NOT_PROVEN", r.blockers)
        self.assertFalse(r.promote)
        # A provider-native approval that explicitly covers both deployments is accepted.
        src = FakeSources()
        ev_b = seal(envelope(src, dep="dep-B"), src, covered=["dep-A", "dep-B"])
        src.approvals[ref]["deployment_id"] = "dep-A"
        self.assertTrue(live(ev_b, src).promote, live(ev_b, src).blockers)

    def test_approval_before_prelive_evidence_or_bound_to_other_evidence(self):
        src = FakeSources()
        ev = seal(envelope(src), src, approved_at=ago(hours=1, minutes=30))  # before runtime line
        self.assertIn("HUMAN_APPROVAL_NOT_PROVEN", live(ev, src).blockers)
        src = FakeSources()
        ev = envelope(src)
        ev["ci"]["result"] = "PASS "                         # envelope edited after approval
        self.assertIn("HUMAN_APPROVAL_NOT_PROVEN", live(ev, src).blockers)
        src = FakeSources()
        ev = envelope(src)
        src.approvals["env-approval-77"]["research_artifact_sha256"] = "1" * 64
        self.assertIn("HUMAN_APPROVAL_NOT_PROVEN", live(ev, src).blockers)


# ───────────────────────── provenance (P1–P10, 5f5757f) ─────────────────────────

class FabricatedPayloadIsNotProvenance(unittest.TestCase):          # P1
    def test_correct_hash_without_trusted_source_blocks(self):
        src = FakeSources()
        ev = envelope(src)
        pa.parse(src.logs["dep-A"][0])
        for sources in (None, type("Untrusted", (), {"source_kind": "LOCAL_JSON"})()):
            r = live(ev, sources)
            self.assertFalse(r.promote)
            self.assertIn("LIVE_PROVENANCE_SOURCE_UNAVAILABLE", r.blockers)
            self.assertFalse(r.to_dict()["production_ready"])

    def test_line_absent_from_the_deployment_logs_blocks(self):
        src = FakeSources()
        ev = envelope(src)
        src.logs["dep-A"] = ["some unrelated log line"]
        r = live(ev, src)
        self.assertIn("RUNTIME_POLICY_OBSERVATION_NOT_FOUND_IN_DEPLOYMENT_LOGS", r.blockers)
        self.assertEqual(r.stages["evidence_components"]["LIVE_PROVENANCE_AUTHENTICATED"], "BLOCK")


class DeploymentBinding(unittest.TestCase):                           # P2
    def test_deployment_a_evidence_cannot_authorize_deployment_b(self):
        src = FakeSources()
        ev = envelope(src)
        ev["railway"]["deployment_id"] = "dep-B"
        src.deployments["dep-B"]["commit_sha"] = OTHER_SHA
        src.logs["dep-B"] = list(src.logs["dep-A"])          # A's line copied into B's logs
        r = live(seal(ev, src), src)
        self.assertFalse(r.promote)
        self.assertIn("RUNTIME_POLICY_OBSERVATION_NOT_FOUND_IN_DEPLOYMENT_LOGS", r.blockers)
        self.assertIn("RAILWAY_DEPLOYMENT_COMMIT_SHA_NOT_CONFIRMED", r.blockers)


class ShaBinding(unittest.TestCase):                                  # P3
    def test_sha_a_runtime_cannot_authorize_sha_b(self):
        src = FakeSources()
        src.logs["dep-A"] = [runtime_line(sha=OTHER_SHA)]
        r = live(envelope(src), src)
        self.assertIn("EXACT_DEPLOYMENT_SHA_NOT_PROVEN", r.blockers)
        self.assertFalse(r.promote)


class StaleEvidence(unittest.TestCase):                               # P4
    def test_old_runtime_observation(self):
        age = dt.timedelta(seconds=lre.MAX_RUNTIME_OBSERVATION_AGE_S + 60)
        src = FakeSources()
        src.logs["dep-A"] = [runtime_line(at=NOW - age)]
        src.deployments["dep-A"]["created_at"] = iso(NOW - age - dt.timedelta(hours=1))
        self.assertIn("RUNTIME_OBSERVATION_STALE", live(envelope(src), src).blockers)

    def test_observation_before_the_deployment(self):
        src = FakeSources()
        src.logs["dep-A"] = [runtime_line(at=NOW - dt.timedelta(hours=3))]
        self.assertIn("RUNTIME_OBSERVATION_PREDATES_DEPLOYMENT", live(envelope(src), src).blockers)

    def test_old_approval_and_ci(self):
        src = FakeSources()
        ev = seal(envelope(src), src, approved_at=ago(days=2))
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


class WrongEnvironmentOrService(unittest.TestCase):                   # P5
    def test_deployment_in_wrong_environment(self):
        src = FakeSources()
        src.deployments["dep-A"]["environment_id"] = "env-staging"
        self.assertIn("WRONG_ENVIRONMENT", live(envelope(src), src).blockers)

    def test_runtime_line_from_another_service(self):
        src = FakeSources()
        src.logs["dep-A"] = [runtime_line(svc="svc-other")]
        self.assertIn("WRONG_SERVICE", live(envelope(src), src).blockers)


class ProtectionStringIsNotEvidence(unittest.TestCase):               # P6
    def test_bare_pass_string(self):
        src = FakeSources()
        ev = envelope(src)
        ev["protection"] = "PASS"
        self.assertIn("RELEASE_EVIDENCE_MALFORMED", live(ev, src).blockers)
        with self.assertRaises(TypeError):
            gate.evaluate_live(artifact(), None, protection_readiness="PASS")


class HumanApprovalMustBeStructured(unittest.TestCase):               # P7
    def test_arbitrary_string(self):
        src = FakeSources()
        ev = envelope(src)
        ev["human_approval"] = "operator said yes"
        self.assertIn("RELEASE_EVIDENCE_MALFORMED", live(ev, src).blockers)
        with self.assertRaises(TypeError):
            gate.evaluate_live(artifact(), None, human_authorization="operator")

    def test_unknown_or_unapproved_record(self):
        src = FakeSources()
        ev = envelope(src)
        src.approvals.clear()
        self.assertIn("HUMAN_APPROVAL_NOT_PROVEN", live(ev, src).blockers)
        src = FakeSources()
        ev = envelope(src)
        src.approvals["env-approval-77"]["state"] = "PENDING"
        self.assertIn("HUMAN_APPROVAL_NOT_PROVEN", live(ev, src).blockers)


class CliShaIsNotIdentity(unittest.TestCase):                         # P8
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
        ev = envelope(src)
        src.deployments["dep-A"]["commit_sha"] = OTHER_SHA
        r = live(ev, src)
        self.assertIn("RAILWAY_DEPLOYMENT_COMMIT_SHA_NOT_CONFIRMED", r.blockers)
        self.assertIn("EXACT_DEPLOYMENT_SHA_NOT_PROVEN", r.blockers)


class ResearchIndependentOfProvenance(unittest.TestCase):            # P9
    def test_research_gate_ignores_live_evidence(self):
        self.assertNotIn("evidence", inspect.signature(gate.evaluate).parameters)
        for status in (gate.POLICY_OBSERVATION_PENDING, gate.POLICY_CONTENT_MATCH):
            a = artifact()
            a["policy_parity"] = status
            r = gate.evaluate(a)
            self.assertTrue(r.promote, r.blockers)
            self.assertFalse(r.to_dict()["production_ready"])
            self.assertFalse(gate.evaluate_live(a, None).promote)


class NoMutation(unittest.TestCase):                                  # P10
    def test_only_read_methods_are_called(self):
        src = FakeSources()
        out = lre.verify(envelope(src, protection="CERTIFICATE"), sources=src, now=NOW)
        lre.verify(envelope(src), sources=src, now=NOW)
        self.assertEqual(out["mutations_performed"], 0)
        self.assertTrue(set(src.calls) <= set(lre.READ_METHODS))
        self.assertEqual(set(src.calls), set(lre.READ_METHODS))

    def test_verifier_source_has_no_mutating_calls(self):
        src = inspect.getsource(lre) + inspect.getsource(gate.evaluate_live) + inspect.getsource(gate.main)
        for pattern in (r"create_order", r"place_order", r"\.post\(", r"\.put\(", r"\.delete\(",
                        r"redeploy\w*\(", r"set_variables", r"restart\w*\(", r"subprocess", r"requests",
                        r"httpx", r"urllib", r"aiohttp", r"socket",
                        r"^\s*(import|from)\s+\S*(kucoin|exchange|ccxt|railway)"):
            self.assertIsNone(re.search(pattern, src, re.IGNORECASE | re.MULTILINE), pattern)


if __name__ == "__main__":
    unittest.main()
