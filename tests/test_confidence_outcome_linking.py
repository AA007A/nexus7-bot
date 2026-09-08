import pytest

from bot.confidence_outcome_linking import (
    outcome_label_from_r,
    select_evidence_for_trade,
)


def test_selects_newest_exact_prior_match():
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
    assert match is not None
    assert match.evidence_id == 2
    assert match.age_seconds == 10.0


def test_rejects_future_stale_wrong_mode_or_already_linked():
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
    assert select_evidence_for_trade(
        rows, symbol="BTCUSDT", side="LONG", entry=100.0, stop_loss=98.0,
        opened_ts=120.0, mode="PAPER", max_age_seconds=60,
    ) is None


def test_rejects_fuzzy_trade_geometry():
    rows = [{"id": 1, "timestamp": "100", "symbol": "BTCUSDT", "proposed_side": "LONG",
             "approved": 1, "entry": 100.001, "stop_loss": 98.0, "mode": "PAPER", "trade_id": None}]
    assert select_evidence_for_trade(
        rows, symbol="BTCUSDT", side="LONG", entry=100.0, stop_loss=98.0,
        opened_ts=120.0, mode="PAPER",
    ) is None


def test_outcome_label_uses_positive_r_only():
    assert outcome_label_from_r(1.2) == 1
    assert outcome_label_from_r(0.01) == 1
    assert outcome_label_from_r(0.0) == 0
    assert outcome_label_from_r(-0.5) == 0


def test_invalid_r_rejected():
    with pytest.raises(ValueError):
        outcome_label_from_r(float("nan"))
