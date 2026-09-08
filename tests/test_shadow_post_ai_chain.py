import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import shadow_live
from bot.risk_manager_v3 import RiskManagerV3
from bot.professional_risk import CapitalState


class _Risk:
    def __init__(self):
        self.balance_confirmed = True
        self.drawdown = 0.0


class _Pilot:
    enabled = False

    def evaluate(self, *args, **kwargs):
        return []


class _Client:
    def __init__(self, *, equity=100.0, available=100.0):
        self.equity = equity
        self.available = available
        self._klines = [
            {"c": 100.0, "h": 101.0, "l": 99.0, "v": 1000.0}
            for _ in range(30)
        ]

    def get_cached_klines(self, *args, **kwargs):
        return list(self._klines)

    async def get_klines(self, *args, **kwargs):
        return list(self._klines)

    def get_cached_ticker(self, symbol):
        return {"lastPrice": 100.0, "bid": 99.99, "ask": 100.01}

    async def get_ticker(self, symbol):
        return self.get_cached_ticker(symbol)

    async def get_orderbook(self, symbol, depth=20):
        return {
            "b": [["99.99", "100000"]],
            "a": [["100.01", "100000"]],
        }

    async def get_positions(self):
        return []

    async def _get(self, path, params=None, auth=False):
        if path == "/api/v1/account-overview":
            return {
                "accountEquity": self.equity,
                "availableBalance": self.available,
                "positionMargin": 0.0,
                "orderMargin": 0.0,
                "unrealisedPNL": 0.0,
            }
        if path == "/api/v1/orders":
            return {"items": []}
        raise AssertionError(path)


class _Engine:
    def __init__(self, *, equity=100.0, available=100.0):
        self.viable_symbols = {"BTCUSDT"}
        self._durable_state_enforced = False
        self.risk = _Risk()
        self.pilot = _Pilot()
        self.client = _Client(equity=equity, available=available)
        self.instruments = {
            "BTCUSDT": {
                "multiplier": 0.001,
                "lotSize": 1,
                "minQty": 1,
                "minNotional": 0,
            }
        }

    async def _nexus_validate(self, sig):
        return SimpleNamespace(
            execution_allowed=True,
            reasoning=["approved fixture"],
            warnings=[],
        )

    def _effective_risk_pct(self):
        return 0.01


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
        self.assertGreater(record["risk_budget"], 0)
        self.assertLessEqual(record["projected_stop_loss"], record["risk_budget"])
        self.assertGreater(record["required_margin"], 0)
        self.assertIn(record["sizing_binding"], {"RISK_BUDGET", "AVAILABLE_COLLATERAL"})

    async def test_tiny_available_collateral_blocks_v3_before_pretrade(self):
        engine = _Engine(available=0.0001)
        state = {"equity": 100.0, "available": 0.0001}
        scorer = AsyncMock(return_value={"aprovado": True, "total": 80})

        with patch.object(shadow_live, "decision_validation_error", return_value=None), \
             patch.object(shadow_live.balance_semantics, "refresh_shadow_risk", new=AsyncMock(return_value=state)), \
             patch.object(shadow_live.scoring, "calculate", new=scorer):
            record = await shadow_live.evaluate_candidate(engine, SIG)

        self.assertIsNone(record)
        scorer.assert_not_awaited()
        self.assertIsInstance(engine._shadow_risk_v3, RiskManagerV3)
        self.assertEqual(engine._shadow_risk_v3.available_collateral, 0.0001)

    async def test_drawdown_limit_blocks_before_v3_sizing(self):
        engine = _Engine(equity=100.0, available=100.0)
        risk_v3 = RiskManagerV3()
        risk_v3.update_capital(CapitalState(100.0, 100.0))
        engine._shadow_risk_v3 = risk_v3
        reduced_equity = 100.0 * (1.0 - float(shadow_live.cfg.MAX_DRAWDOWN) - 0.01)
        engine.client.equity = reduced_equity
        state = {"equity": reduced_equity, "available": 100.0}
        scorer = AsyncMock(return_value={"aprovado": True, "total": 80})

        with patch.object(shadow_live, "decision_validation_error", return_value=None), \
             patch.object(shadow_live.balance_semantics, "refresh_shadow_risk", new=AsyncMock(return_value=state)), \
             patch.object(shadow_live.scoring, "calculate", new=scorer):
            record = await shadow_live.evaluate_candidate(engine, SIG)

        self.assertIsNone(record)
        self.assertGreaterEqual(engine._shadow_risk_v3.drawdown, shadow_live.cfg.MAX_DRAWDOWN)
        scorer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
