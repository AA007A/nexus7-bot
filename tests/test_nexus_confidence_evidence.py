import unittest
from types import SimpleNamespace

from bot.nexus_confidence_evidence import normalize_evidence, capture_decision


class _FakeDB:
    def __init__(self):
        self._is_pg = False
        self.calls = []

    async def _exec(self, sql, params=()):
        self.calls.append((sql, params))
        return True


def _decision(**overrides):
    data = dict(
        symbol="ATOMUSDT",
        decision="LONG",
        execution_allowed=True,
        confidence=78.0,
        setup_quality=82.0,
        data_quality=95.0,
        expected_value=0.42,
        risk_reward=2.1,
        entry=1.8,
        stop_loss=1.76,
        take_profit=1.88,
        market_regime="TRENDING_BULL",
        setup_grade="B",
    )
    data.update(overrides)
    return SimpleNamespace(**data)


class NexusConfidenceEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_normalization_preserves_confidence_and_ev(self):
        row = normalize_evidence(
            nx_dec=_decision(), proposed_side="LONG", decision_source="nexus_ai", mode="SHADOW"
        )
        self.assertEqual(row["confidence"], 78.0)
        self.assertEqual(row["expected_value"], 0.42)
        self.assertEqual(row["approved"], 1)
        self.assertEqual(row["mode"], "SHADOW")

    def test_invalid_confidence_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_evidence(
                nx_dec=_decision(confidence=101.0), proposed_side="LONG",
                decision_source="nexus_ai", mode="SHADOW"
            )

    async def test_capture_writes_ddl_and_insert_only(self):
        db = _FakeDB()
        ok = await capture_decision(
            db, nx_dec=_decision(), proposed_side="LONG",
            decision_source="nexus_ai", mode="SHADOW"
        )
        self.assertTrue(ok)
        self.assertEqual(len(db.calls), 2)
        self.assertIn("CREATE TABLE IF NOT EXISTS nexus_confidence_evidence", db.calls[0][0])
        self.assertIn("INSERT INTO nexus_confidence_evidence", db.calls[1][0])
        combined = " ".join(sql.upper() for sql, _ in db.calls)
        for forbidden in ("PLACE_ORDER", "CANCEL", "SET_POSITION", "LEVERAGE"):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
