import json
import unittest

from bot import hwm_provenance as hp


class HwmProvenanceTests(unittest.TestCase):
    def test_builds_non_secret_auditable_snapshot(self):
        payload = json.loads(hp.build_hwm_provenance(
            reason="new_equity_high",
            old_peak=30.0,
            new_peak=31.0,
            account_equity=31.0,
            evidence_ref="authenticated_account_equity",
        ))
        self.assertEqual(payload["reason"], "new_equity_high")
        self.assertEqual(payload["old_peak"], 30.0)
        self.assertEqual(payload["new_peak"], 31.0)
        self.assertEqual(payload["execution_effect"], "NONE")
        self.assertIn("recorded_at", payload)

    def test_rejects_unknown_reason(self):
        with self.assertRaises(ValueError):
            hp.build_hwm_provenance(
                reason="manual_reset", old_peak=30.0, new_peak=20.0,
                account_equity=20.0, evidence_ref="operator",
            )

    def test_rejects_empty_evidence(self):
        with self.assertRaises(ValueError):
            hp.build_hwm_provenance(
                reason="bootstrap", old_peak=None, new_peak=20.0,
                account_equity=20.0, evidence_ref="",
            )

    def test_rejects_boolean_numeric_fields(self):
        for field in ("new_peak", "account_equity", "old_peak"):
            kwargs = dict(
                reason="new_equity_high", old_peak=30.0, new_peak=31.0,
                account_equity=31.0, evidence_ref="authenticated_account_equity",
            )
            kwargs[field] = True
            with self.subTest(field=field), self.assertRaises(ValueError):
                hp.build_hwm_provenance(**kwargs)


if __name__ == "__main__":
    unittest.main()
