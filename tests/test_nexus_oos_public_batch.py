import tempfile
import unittest
from pathlib import Path

from bot.nexus_oos_edge_gate import CandidateOutcome
import bot.nexus_oos_public_batch as batch


class _PublicClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _report(symbol, *, parity=False, approved=True, r=1.0, ts=1000.0):
    return {
        "symbol": symbol,
        "candles_15m": 2500,
        "funding_events": 10,
        "approved": int(approved),
        "rejected": int(not approved),
        "candidates": [
            CandidateOutcome(ts, approved, True, 0.8 if approved else 0.4, True, r)
        ],
        "historical_context": {
            "candles": True,
            "ticker_proxy": True,
            "funding_history": True,
            "historical_open_interest": parity,
            "historical_orderbook": parity,
            "parity_complete": parity,
        },
    }


def _stable_report(symbol, offset, *, parity=True):
    rows = []
    for i in range(160):
        approved = i % 2 == 0
        rows.append(
            CandidateOutcome(
                float(offset + i), approved, True,
                0.8 if approved else 0.4, True,
                1.0 if approved else -0.8,
            )
        )
    return {
        "symbol": symbol,
        "candles_15m": 2500,
        "funding_events": 10,
        "approved": sum(1 for row in rows if row.approved),
        "rejected": sum(1 for row in rows if not row.approved),
        "candidates": rows,
        "historical_context": {
            "candles": True,
            "ticker_proxy": True,
            "funding_history": True,
            "historical_open_interest": parity,
            "historical_orderbook": parity,
            "parity_complete": parity,
        },
    }


class NexusOOSPublicBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_client = batch.PublicKuCoinFuturesClient
        self.old_replay = batch.replay_symbol
        batch.PublicKuCoinFuturesClient = _PublicClient

    async def asyncTearDown(self):
        batch.PublicKuCoinFuturesClient = self.old_client
        batch.replay_symbol = self.old_replay

    def test_default_universe_contains_all_12_live_symbols(self):
        self.assertEqual(len(batch.DEFAULT_SYMBOLS), 12)
        self.assertEqual(len(set(batch.DEFAULT_SYMBOLS)), 12)
        for symbol in ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "ATOMUSDT"):
            self.assertIn(symbol, batch.DEFAULT_SYMBOLS)

    async def test_missing_historical_context_forces_not_proven(self):
        async def replay(client, symbol, *, limit_15m):
            return _report(symbol, parity=False)

        batch.replay_symbol = replay
        with tempfile.TemporaryDirectory() as tmp:
            payload = await batch.run_public_batch(
                ["BTCUSDT", "ETHUSDT"],
                output_dir=tmp,
                bootstrap_samples=100,
            )
        self.assertEqual(payload["status"], "AI_EDGE_NOT_PROVEN")
        self.assertFalse(payload["proven"])
        self.assertIn("HISTORICAL_CONTEXT_PARITY_INCOMPLETE", payload["blockers"])
        self.assertFalse(payload["private_credentials_required"])
        self.assertEqual(payload["execution_effect"], "NONE")

    async def test_requested_symbol_failure_is_explicit_blocker(self):
        async def replay(client, symbol, *, limit_15m):
            if symbol == "ETHUSDT":
                return {"symbol": symbol, "error": "insufficient_history", "candidates": []}
            return _report(symbol, parity=False)

        batch.replay_symbol = replay
        with tempfile.TemporaryDirectory() as tmp:
            payload = await batch.run_public_batch(
                ["BTCUSDT", "ETHUSDT"],
                output_dir=tmp,
                bootstrap_samples=100,
                require_all_symbols=True,
            )
        self.assertIn("REQUESTED_SYMBOL_REPLAY_INCOMPLETE", payload["blockers"])
        self.assertEqual(payload["successful_symbols"], ["BTCUSDT"])
        self.assertEqual(payload["failed_symbols"][0]["symbol"], "ETHUSDT")

    async def test_full_parity_still_cannot_bypass_statistical_sample_gate(self):
        async def replay(client, symbol, *, limit_15m):
            return _report(symbol, parity=True, approved=(symbol == "BTCUSDT"), r=1.0)

        batch.replay_symbol = replay
        with tempfile.TemporaryDirectory() as tmp:
            payload = await batch.run_public_batch(
                ["BTCUSDT", "ETHUSDT"],
                output_dir=tmp,
                bootstrap_samples=100,
            )
        self.assertTrue(payload["historical_context_parity_complete"])
        self.assertEqual(payload["status"], "AI_EDGE_NOT_PROVEN")
        self.assertIn("INSUFFICIENT_BASELINE_OOS_SAMPLE", payload["blockers"])

    async def test_robustness_uses_same_requested_symbol_universe(self):
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
        offsets = {symbol: idx * 1000 for idx, symbol in enumerate(symbols)}

        async def replay(client, symbol, *, limit_15m):
            return _stable_report(symbol, offsets[symbol], parity=True)

        batch.replay_symbol = replay
        with tempfile.TemporaryDirectory() as tmp:
            payload = await batch.run_public_batch(
                symbols,
                output_dir=tmp,
                bootstrap_samples=300,
                temporal_folds=4,
            )
            self.assertTrue((Path(tmp) / "nexus_oos_robustness.json").is_file())
        summary = payload["robustness"]["summary"]
        self.assertEqual(summary["symbols_evaluated"], len(symbols))
        self.assertEqual(summary["promotion_role"], "BLOCK_ONLY")
        self.assertEqual(payload["methodology"]["robustness_symbol_universe"], "same_as_requested_batch")
        self.assertEqual(payload["robustness_blockers"], [])


if __name__ == "__main__":
    unittest.main()
