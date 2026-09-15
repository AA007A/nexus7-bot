import json
import unittest
from unittest.mock import AsyncMock, patch

from bot import hwm_provenance as hp


class HwmProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_non_secret_auditable_snapshot(self):
        with patch.object(hp.db, "save_key_value", AsyncMock(return_value=True)) as save:
            ok = await hp.persist_hwm_provenance(
                reason="new_equity_high",
                old_peak=30.0,
                new_peak=31.0,
                account_equity=31.0,
                evidence_ref="authenticated_account_equity",
                strict=True,
            )
        self.assertTrue(ok)
        self.assertEqual(save.await_args.args[0], hp.HWM_PROVENANCE_KEY)
        payload = json.loads(save.await_args.args[1])
        self.assertEqual(payload["reason"], "new_equity_high")
        self.assertEqual(payload["old_peak"], 30.0)
        self.assertEqual(payload["new_peak"], 31.0)
        self.assertEqual(payload["execution_effect"], "NONE")

    async def test_rejects_unknown_reason(self):
        with self.assertRaises(ValueError):
            await hp.persist_hwm_provenance(
                reason="manual_reset",
                old_peak=30.0,
                new_peak=20.0,
                account_equity=20.0,
                evidence_ref="operator",
            )

    async def test_rejects_empty_evidence(self):
        with self.assertRaises(ValueError):
            await hp.persist_hwm_provenance(
                reason="bootstrap",
                old_peak=None,
                new_peak=20.0,
                account_equity=20.0,
                evidence_ref="",
            )


if __name__ == "__main__":
    unittest.main()
