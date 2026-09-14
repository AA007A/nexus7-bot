import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from bot import daily_pnl_estimate_lineage_hardening as hardening
from bot import durable_daily_pnl as pnl


class DummyLog:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class EstimateLineageHardeningTests(unittest.TestCase):
    def _trade(self):
        return SimpleNamespace(
            symbol="NEARUSDT",
            direction="LONG",
            entry=2.463740157,
            exit_price=2.498754961,
            qty=158.8,
            pnl=8.5,
            opened_at=datetime(2026, 9, 14, 17, 0, 8, tzinfo=timezone.utc),
            closed_at=datetime(2026, 9, 14, 17, 10, 11, tzinfo=timezone.utc),
        )

    def test_enrich_estimate_marks_source_and_exact_opening_lineage(self):
        trade = self._trade()
        pos = SimpleNamespace(_forensic_lineage={"order_id": "open-near-1"})
        engine = SimpleNamespace(positions={"NEARUSDT": pos})

        result = hardening.enrich_estimate(engine, trade)

        self.assertIs(result, trade)
        self.assertEqual(trade.accounting_source, "ESTIMATED_LOCAL_MARK_AND_FEE_RATE")
        self.assertEqual(trade.opening_order_id, "open-near-1")

    def test_missing_lineage_is_not_invented_but_trade_remains_estimate(self):
        trade = self._trade()
        engine = SimpleNamespace(positions={"NEARUSDT": SimpleNamespace()})

        hardening.enrich_estimate(engine, trade)

        self.assertEqual(trade.accounting_source, "ESTIMATED_LOCAL_MARK_AND_FEE_RATE")
        self.assertFalse(hasattr(trade, "opening_order_id"))

    def test_no_matching_position_leaves_trade_untouched(self):
        trade = self._trade()
        engine = SimpleNamespace(positions={})

        hardening.enrich_estimate(engine, trade)

        self.assertFalse(hasattr(trade, "accounting_source"))
        self.assertFalse(hasattr(trade, "opening_order_id"))

    def test_partial_then_rr_double_estimate_matches_kucoin_by_opening_order(self):
        # The remaining quantity after a partial exit is irrelevant to identity:
        # exact opening lineage must still bind the final operational estimate to
        # the authoritative KuCoin closed-position receipt.
        trade = self._trade()
        pos = SimpleNamespace(
            qty=158.8,
            qty_original=317.5,
            tp1_hit=True,
            _forensic_lineage={"order_id": "open-near-final"},
        )
        engine = SimpleNamespace(positions={"NEARUSDT": pos})
        hardening.enrich_estimate(engine, trade)
        token, value = pnl.event(trade)
        rows = {token: value}
        row = {
            "closeId": "close-near-final",
            "symbol": "NEARUSDTM",
            "closeTime": int(datetime(2026, 9, 14, 17, 10, 11, tzinfo=timezone.utc).timestamp() * 1000),
            "pnl": "10.17184468",
        }
        receipt = {"opening_order_ids": ["open-near-final"]}

        match = pnl._confirmed_adjustment(rows, row, receipt)

        self.assertIsNotNone(match)
        _, adjustment = match
        self.assertEqual(adjustment["opening_order_id"], "open-near-final")
        self.assertEqual(adjustment["estimated_event"], token)
        self.assertAlmostEqual(adjustment["confirmed_pnl"], 10.17184468)
        self.assertAlmostEqual(adjustment["pnl"], 10.17184468 - trade.pnl)

    def test_installed_checkpoint_enriches_before_first_persistence_call(self):
        calls = []

        class DummyPnl:
            _estimate_lineage_hardening_installed = False

            @staticmethod
            async def checkpoint(engine, extra=None, now=None):
                calls.append(
                    (
                        getattr(extra, "accounting_source", None),
                        getattr(extra, "opening_order_id", None),
                        now,
                    )
                )
                return True

        trade = self._trade()
        engine = SimpleNamespace(
            positions={
                "NEARUSDT": SimpleNamespace(
                    _forensic_lineage={"order_id": "open-before-write"}
                )
            }
        )
        hardening.install(DummyPnl, DummyLog())
        now = datetime(2026, 9, 14, 17, 10, 12, tzinfo=timezone.utc)

        result = asyncio.run(DummyPnl.checkpoint(engine, extra=trade, now=now))

        self.assertTrue(result)
        self.assertEqual(
            calls,
            [("ESTIMATED_LOCAL_MARK_AND_FEE_RATE", "open-before-write", now)],
        )


if __name__ == "__main__":
    unittest.main()
