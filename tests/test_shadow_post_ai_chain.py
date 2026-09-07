import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import shadow_live


class _Risk:
    def __init__(self, drawdown=0.0, qty=1.0):
        self.balance_confirmed = True
        self.drawdown = drawdown
        self._qty = qty
        self.size_calls = 0

    def size(self, *args, **kwargs):
        self.size_calls += 1
        return self._qty


class _Pilot:
    enabled = False

    def evaluate(self, *args, **kwargs):
        return []


class _Client:
    def __init__(self):
        self._klines = [
            {"c": 100.0, "h": 101.0, "l": 99.0, "v": 1000.0}
            for _ in range(30)
        ]

    def get_cached_klines(self, *args, **kwargs):
        return list(self._klines)

    async def get_klines(self, *args, **kwargs):
        return list(self._klines)


class _Engine:
    def __init__(self, drawdown=0.0, qty=1.0):
        self.viable_symbols = {"BTCUSDT"}
        self._durable_state_enforced = False
        self.risk = _Risk(drawdown=drawdown, qty=qty)
        self.pilot = _Pilot()
        self.client = _Client()
        self.instruments = {"BTCUSDT": {}}

    async def _nexus_validate(self, sig):
        return SimpleNamespace(
            execution_allowed=True,
            reasoning=["approved fixture"],
            warnings=[],
        )


SIG = SimpleNamespace(
    symbol="BTCUSDT",
    direction="LONG",
    entry=100.0,
    sl=95.0,
    tp=110.0,
    score=70,
)


class ShadowPostAIChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_candidate_reaches_would_submit_record_without_execution(self):
        engine = _Engine()
        state = {"equity": 100.0, "available": 100.0}
        liq_ok = SimpleNamespace(stop_effective=True)

        with patch.object(shadow_live, "decision_validation_error", return_value=None), \
             patch.object(shadow_live.balance_semantics, "refresh_shadow_risk", new=AsyncMock(return_value=state)), \
             patch.object(shadow_live.balance_semantics, "collateral_allows", return_value=(True, 1.01)), \
             patch.object(shadow_live.scoring, "calculate", new=AsyncMock(return_value={"aprovado": True, "total": 80})), \
             patch.object(shadow_live, "validate_base_quantity", return_value=None), \
             patch.object(shadow_live.liq, "analyze", return_value=liq_ok):
            record = await shadow_live.evaluate_candidate(engine, SIG)

        self.assertIsNotNone(record)
        self.assertEqual(record["execution_effect"], "NONE")
        self.assertTrue(record["ai_approved"])
        self.assertEqual(record["symbol"], "BTCUSDT")
        self.assertEqual(record["side"], "LONG")
        self.assertEqual(record["pretrade"], 80)
        self.assertEqual(engine.risk.size_calls, 1)

    async def test_low_available_collateral_blocks_after_ai_before_pretrade(self):
        engine = _Engine()
        state = {"equity": 100.0, "available": 0.10}
        scorer = AsyncMock(return_value={"aprovado": True, "total": 80})

        with patch.object(shadow_live, "decision_validation_error", return_value=None), \
             patch.object(shadow_live.balance_semantics, "refresh_shadow_risk", new=AsyncMock(return_value=state)), \
             patch.object(shadow_live.balance_semantics, "collateral_allows", return_value=(False, 1.01)), \
             patch.object(shadow_live.scoring, "calculate", new=scorer):
            record = await shadow_live.evaluate_candidate(engine, SIG)

        self.assertIsNone(record)
        self.assertEqual(engine.risk.size_calls, 1)
        scorer.assert_not_awaited()

    async def test_drawdown_limit_blocks_before_sizing(self):
        engine = _Engine(drawdown=float(shadow_live.cfg.MAX_DRAWDOWN) + 0.001)
        state = {"equity": 100.0, "available": 100.0}

        with patch.object(shadow_live, "decision_validation_error", return_value=None), \
             patch.object(shadow_live.balance_semantics, "refresh_shadow_risk", new=AsyncMock(return_value=state)):
            record = await shadow_live.evaluate_candidate(engine, SIG)

        self.assertIsNone(record)
        self.assertEqual(engine.risk.size_calls, 0)


if __name__ == "__main__":
    unittest.main()
