import unittest

from bot.confidence_outcome_linking import (
    outcome_label_from_r,
    select_evidence_for_trade,
)


class ConfidenceOutcomeLinkingTests(unittest.TestCase):
    def test_selects_newest_exact_prior_match(self):
        rows = [
            {"id": 1, "timestamp": "100", "symbol": "BTCUSDT", "proposed_side": "LONG",
             "approved": 1, "entry": 100.0, "stop_loss": 98.0, "mode": "PAPER", "trade_id": None},
            {"id": 2, "timestamp": "110", "symbol": "BTCUSDT", "proposed_side": "LONG",
             "approved": 1, "entry": 100.0, "stop_loss": 98.0, "mode": "PAPER", "trade_id": None},
        ]
        match = select_evidence_for_trade(
            rows, symbol="BTCUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            opened_ts=120.0, mode="PAPER", max_age_seconds=60,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.evidence_id, 2)
        self.assertEqual(match.age_seconds, 10.0)

    def test_rejects_future_stale_wrong_mode_or_already_linked(self):
        rows = [
            {"id": 1, "timestamp": "121", "symbol": "BTCUSDT", "proposed_side": "LONG",
             "approved": 1, "entry": 100.0, "stop_loss": 98.0, "mode": "PAPER", "trade_id": None},
            {"id": 2, "timestamp": "1", "symbol": "BTCUSDT", "proposed_side": "LONG",
             "approved": 1, "entry": 100.0, "stop_loss": 98.0, "mode": "PAPER", "trade_id": None},
            {"id": 3, "timestamp": "110", "symbol": "BTCUSDT", "proposed_side": "LONG",
             "approved": 1, "entry": 100.0, "stop_loss": 98.0, "mode": "SHADOW", "trade_id": None},
            {"id": 4, "timestamp": "110", "symbol": "BTCUSDT", "proposed_side": "LONG",
             "approved": 1, "entry": 100.0, "stop_loss": 98.0, "mode": "PAPER", "trade_id": 99},
        ]
        self.assertIsNone(select_evidence_for_trade(
            rows, symbol="BTCUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            opened_ts=120.0, mode="PAPER", max_age_seconds=60,
        ))

    def test_rejects_fuzzy_trade_geometry(self):
        rows = [{"id": 1, "timestamp": "100", "symbol": "BTCUSDT", "proposed_side": "LONG",
                 "approved": 1, "entry": 100.001, "stop_loss": 98.0, "mode": "PAPER", "trade_id": None}]
        self.assertIsNone(select_evidence_for_trade(
            rows, symbol="BTCUSDT", side="LONG", entry=100.0, stop_loss=98.0,
            opened_ts=120.0, mode="PAPER",
        ))

    def test_outcome_label_uses_positive_r_only(self):
        self.assertEqual(outcome_label_from_r(1.2), 1)
        self.assertEqual(outcome_label_from_r(0.01), 1)
        self.assertEqual(outcome_label_from_r(0.0), 0)
        self.assertEqual(outcome_label_from_r(-0.5), 0)

    def test_invalid_r_rejected(self):
        with self.assertRaises(ValueError):
            outcome_label_from_r(float("nan"))


if __name__ == "__main__":
    unittest.main()
