"""Static contract for the production promotion boundary."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


class ProductionPromotionPipelineTests(unittest.TestCase):
    def test_main_quality_and_security_are_push_gates(self):
        quality = _read("quality.yml")
        security = _read("security.yml")

        for text in (quality, security):
            self.assertIn("push:", text)
            self.assertIn("branches: [main]", text)

    def test_attestation_composes_quality_and_security_for_exact_sha(self):
        attestation = _read("ci_attest.yml")

        self.assertIn('workflows: ["Quality Check"]', attestation)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", attestation)
        self.assertIn("github.event.workflow_run.event == 'push'", attestation)
        self.assertIn("Require matching Supply Chain Security success", attestation)
        self.assertIn('passed/${ATTEST_SHA,,}.txt', attestation)

    def test_promotion_is_manual_exact_main_and_non_force(self):
        promotion = _read("promote_production.yml")

        self.assertIn("workflow_dispatch:", promotion)
        self.assertNotIn("workflow_run:", promotion)
        self.assertNotIn("branches: [main]", promotion)
        self.assertIn('candidate="${CANDIDATE_SHA,,}"', promotion)
        self.assertIn('candidate" != "$main_sha', promotion)
        self.assertIn('origin/ci-attestations:passed/${candidate}.txt', promotion)
        self.assertIn(
            'git push origin "$PROMOTION_SHA:refs/heads/production"', promotion
        )
        self.assertNotIn("--force", promotion)

    def test_production_push_has_an_independent_exact_sha_gate(self):
        release_gate = _read("production_release_gate.yml")

        self.assertIn("push:", release_gate)
        self.assertIn("branches: [production]", release_gate)
        self.assertIn(
            'git merge-base --is-ancestor "$promoted" origin/main', release_gate
        )
        self.assertIn('origin/ci-attestations:passed/${promoted}.txt', release_gate)

    def test_railway_predeploy_gate_is_preserved_in_release_contract(self):
        documentation = (ROOT / "docs" / "PRODUCTION_PROMOTION.md").read_text(
            encoding="utf-8"
        )
        deploy_gate = (ROOT / "bot" / "ci_deploy_gate.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("python -m bot.ci_deploy_gate", documentation)
        self.assertIn("checkSuites=true", documentation)
        self.assertIn("exact-SHA", deploy_gate)
