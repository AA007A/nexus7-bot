import unittest

from bot.nexus_runtime_engine import TradingEngine as RuntimeTradingEngine
from bot.professional_risk_adapter import ProfessionalRiskAdapter


class _LegacyRisk:
    def __init__(self):
        self.balance = 100.0
        self.balance_confirmed = True
        self.drawdown = 0.0

    def update(self, balance):
        self.balance = float(balance)
        self.balance_confirmed = True


class _Client:
    def __init__(self, *, positions=None, orders=None, fail_positions=False,
                 fail_orders=False):
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.fail_positions = fail_positions
        self.fail_orders = fail_orders
        self.balance_reads = 0
        self.position_reads = 0
        self.order_reads = 0

    async def get_balance(self):
        self.balance_reads += 1
        return 100.0

    async def get_positions(self):
        self.position_reads += 1
        if self.fail_positions:
            raise RuntimeError("positions unavailable")
        return list(self.positions)

    async def _get(self, path, params=None, auth=False):
        self.order_reads += 1
        if self.fail_orders:
            raise RuntimeError("orders unavailable")
        return {"items": list(self.orders)}


def _engine(*, paper=False, shadow=False, client=None, symbol="BTCUSDT"):
    engine = object.__new__(RuntimeTradingEngine)
    engine.client = client or _Client()
    engine.risk = ProfessionalRiskAdapter(_LegacyRisk())
    engine.paper_trade = bool(paper)
    engine._validation_safety_lock_active = bool(shadow)
    engine._professional_risk_candidate_symbol = str(symbol)
    return engine


class CoreFinalExposureRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_account_passes_after_fresh_balance(self):
        client = _Client()
        engine = _engine(client=client)
        self.assertTrue(await RuntimeTradingEngine._refresh_entry_balance(engine))
        self.assertEqual(client.balance_reads, 1)
        self.assertEqual(client.position_reads, 1)
        self.assertEqual(client.order_reads, 1)

    async def test_existing_candidate_position_blocks(self):
        client = _Client(positions=[{"symbol": "BTCUSDT", "size": 1}])
        engine = _engine(client=client)
        self.assertFalse(await RuntimeTradingEngine._refresh_entry_balance(engine))

    async def test_any_active_exchange_order_blocks(self):
        client = _Client(orders=[{"id": "external-order"}])
        engine = _engine(client=client)
        self.assertFalse(await RuntimeTradingEngine._refresh_entry_balance(engine))

    async def test_position_or_order_read_failure_blocks(self):
        positions_down = _engine(client=_Client(fail_positions=True))
        orders_down = _engine(client=_Client(fail_orders=True))
        self.assertFalse(
            await RuntimeTradingEngine._refresh_entry_balance(positions_down)
        )
        self.assertFalse(
            await RuntimeTradingEngine._refresh_entry_balance(orders_down)
        )

    async def test_missing_candidate_symbol_blocks_nonpaper(self):
        engine = _engine(symbol="")
        self.assertFalse(await RuntimeTradingEngine._refresh_entry_balance(engine))

    async def test_paper_keeps_exchange_exposure_reads_disabled(self):
        client = _Client(
            positions=[{"symbol": "BTCUSDT", "size": 1}],
            orders=[{"id": "external-order"}],
        )
        engine = _engine(paper=True, client=client)
        self.assertTrue(await RuntimeTradingEngine._refresh_entry_balance(engine))
        self.assertEqual(client.balance_reads, 1)
        self.assertEqual(client.position_reads, 0)
        self.assertEqual(client.order_reads, 0)

    async def test_shadow_keeps_dedicated_pipeline_isolated(self):
        client = _Client(
            positions=[{"symbol": "BTCUSDT", "size": 1}],
            orders=[{"id": "external-order"}],
        )
        engine = _engine(shadow=True, client=client)
        self.assertTrue(await RuntimeTradingEngine._refresh_entry_balance(engine))
        self.assertEqual(client.balance_reads, 1)
        self.assertEqual(client.position_reads, 0)
        self.assertEqual(client.order_reads, 0)

    def test_runtime_recheck_has_no_exchange_mutation_authority(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        text = (root / "bot" / "nexus_runtime_engine.py").read_text(encoding="utf-8")
        for marker in (
            "place_order(",
            "cancel_order(",
            "cancel_all_orders(",
            "set_position_stops(",
            "set_leverage(",
            "close_position(",
            "PILOT_RELEASE_APPROVED",
            "execution_effect=SUBMIT",
            "execution_effect=MUTATE",
        ):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
