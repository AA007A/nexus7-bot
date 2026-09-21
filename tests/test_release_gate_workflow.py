import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release_gate.yml"


class ReleaseGateWorkflowTests(unittest.TestCase):
    def test_release_gate_is_main_push_check(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: Release Gate", text)
        self.assertIn("push:", text)
        self.assertIn("branches: [main]", text)

    def test_release_gate_requires_exact_sha_attestation(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("RELEASE_SHA: ${{ github.sha }}", text)
        self.assertIn("ref=ci-attestations", text)
        self.assertIn('passed/${RELEASE_SHA,,}.txt', text)
        self.assertIn("RELEASE_GATE=PASS", text)

    def test_release_gate_has_no_deploy_action(self):
        text = WORKFLOW.read_text(encoding="utf-8").lower()
        self.assertNotIn("railway up", text)
        self.assertNotIn("railway deploy", text)
        self.assertNotIn("redeploy", text)


if __name__ == "__main__":
    unittest.main()
