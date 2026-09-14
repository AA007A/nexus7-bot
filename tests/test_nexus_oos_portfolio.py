import tempfile
import unittest
from pathlib import Path

from bot.nexus_oos_edge_gate import CandidateOutcome
from bot.nexus_oos_portfolio import (
    build_portfolio_edge_report,
    portfolio_csv,
    portfolio_sha256,
    write_portfolio_evidence_bundle,
)
from bot.nexus_oos_replay import ReplayEvidence


def _replay(symbol, rows):
    return ReplayEvidence(
        symbol=symbol,
        candidates=tuple(rows),
        baseline_trade_count=len(rows),
        evaluated_count=len(rows),
        warmup_excluded_count=0,
    )


class NexusOOSPortfolioTests(unittest.TestCase):
    def test_same_timestamp_across_symbols_is_valid_and_deterministic(self):
        btc = _replay("BTCUSDT", [CandidateOutcome(1000.0, True, True, 0.8, True, 1.0)])
        eth = _replay("ETHUSDT", [CandidateOutcome(1000.0, False, True, 0.4, True, -1.0)])
        text_a = portfolio_csv([btc, eth])
        text_b = portfolio_csv([eth, btc])
        self.assertEqual(text_a, text_b)
        self.assertEqual(portfolio_sha256([btc, eth]), portfolio_sha256([eth, btc]))
        self.assertIn("BTCUSDT,1000", text_a)
        self.assertIn("ETHUSDT,1000", text_a)

    def test_duplicate_same_symbol_timestamp_fails_closed(self):
        replay = _replay(
            "BTCUSDT",
            [
                CandidateOutcome(1000.0, True, True, 0.8, True, 1.0),
                CandidateOutcome(1000.0, False, True, 0.4, True, -1.0),
            ],
        )
        with self.assertRaises(ValueError):
            portfolio_csv([replay])

    def test_cluster_bootstrap_can_prove_positive_portfolio_uplift(self):
        btc_rows = []
        eth_rows = []
        for i in range(120):
            ts = float(i)
            btc_rows.append(CandidateOutcome(ts, True, True, 0.8, True, 1.0))
            eth_rows.append(CandidateOutcome(ts, False, True, 0.4, True, -1.0))
        report = build_portfolio_edge_report(
            [_replay("BTCUSDT", btc_rows), _replay("ETHUSDT", eth_rows)],
            bootstrap_samples=1200,
            seed=11,
        )
        self.assertEqual(report.known_baseline_outcomes, 240)
        self.assertEqual(report.known_approved_outcomes, 120)
        self.assertEqual(report.known_rejected_outcomes, 120)
        self.assertGreater(report.expectancy_uplift_r, 0.0)
        self.assertGreater(report.bootstrap_ci_low_r, 0.0)

    def test_bundle_stays_not_proven_when_sample_is_insufficient(self):
        btc = _replay("BTCUSDT", [CandidateOutcome(1.0, True, True, 0.8, True, 1.0)])
        eth = _replay("ETHUSDT", [CandidateOutcome(1.0, False, True, 0.4, True, -1.0)])
        with tempfile.TemporaryDirectory() as tmp:
            payload = write_portfolio_evidence_bundle(
                [btc, eth],
                tmp,
                bootstrap_samples=100,
                gate_kwargs={
                    "min_baseline_samples": 200,
                    "min_approved_samples": 75,
                    "min_rejected_samples": 75,
                },
            )
            self.assertEqual(payload["status"], "AI_EDGE_NOT_PROVEN")
            self.assertFalse(payload["proven"])
            self.assertEqual(payload["bootstrap_unit"], "decision_timestamp_cluster")
            self.assertEqual(payload["parity"], "CORE_CANDLES_ONLY")
            self.assertTrue(payload["blockers"])
            self.assertTrue((Path(tmp) / "nexus_oos_portfolio_candidates.csv").is_file())
            self.assertTrue((Path(tmp) / "nexus_oos_portfolio_report.json").is_file())


if __name__ == "__main__":
    unittest.main()
