import inspect
from types import SimpleNamespace

from bot import nexus_validation_observability as validation_obs
from bot import rr_gate_calibration as cal
from bot.nexus_live_cost_calibration import NexusCostContext, _COST_CONTEXT


def test_rr_net_bucket_boundaries_are_stable_around_live_floor():
    floor = 1.60
    assert cal._rr_net_bucket(1.10, floor) == "LT_1_20"
    assert cal._rr_net_bucket(1.30, floor) == "1_20_1_40"
    assert cal._rr_net_bucket(1.45, floor) == "1_40_1_50"
    assert cal._rr_net_bucket(1.55, floor) == "1_50_TO_THRESHOLD"
    assert cal._rr_net_bucket(1.60, floor) == "AT_OR_ABOVE_THRESHOLD"
    assert cal._rr_net_bucket(1.90, floor) == "AT_OR_ABOVE_THRESHOLD"


def test_rr_gap_bucket_quantifies_distance_to_threshold():
    floor = 1.60
    assert cal._rr_gap_bucket(1.58, floor) == "GAP_0_00_0_05"
    assert cal._rr_gap_bucket(1.52, floor) == "GAP_0_05_0_10"
    assert cal._rr_gap_bucket(1.45, floor) == "GAP_0_10_0_20"
    assert cal._rr_gap_bucket(1.30, floor) == "GAP_0_20_0_40"
    assert cal._rr_gap_bucket(1.10, floor) == "GAP_GT_0_40"
    assert cal._rr_gap_bucket(1.61, floor) == "AT_OR_ABOVE_THRESHOLD"


def test_reason_parser_reads_canonical_liquid_rr_message():
    reason = "R:R líquido 1.45 < mínimo líquido 1.60 (bruto exigido: 2.0)"
    assert cal._reason_rr_net(reason) == 1.45
    assert cal._reason_rr_net("unrelated veto") is None


def test_snapshot_uses_inherited_nexus_cost_context_without_io():
    sig = SimpleNamespace(
        symbol="LTCUSDT",
        entry=100.0,
        sl=101.0,
        tp=102.0,
    )
    ctx = NexusCostContext(
        symbol="LTCUSDT",
        taker_fee=0.0006,
        slippage=0.0003,
        fee_source="test_fee",
        slippage_source="test_slippage",
        spread_bps=2.0,
    )
    token = _COST_CONTEXT.set(ctx)
    try:
        snapshot = cal._snapshot(sig)
    finally:
        _COST_CONTEXT.reset(token)

    assert snapshot["available"] is True
    assert snapshot["same_nexus_cost_context"] is True
    assert snapshot["rr_net_snapshot"] > 0
    assert snapshot["rr_net_threshold"] == 1.60
    assert snapshot["taker_fee_bps"] == 6.0
    assert snapshot["slippage_bps"] == 3.0
    assert snapshot["fee_source"] == "test_fee"
    assert snapshot["slippage_source"] == "test_slippage"


def test_snapshot_fails_closed_when_cost_context_is_missing():
    sig = SimpleNamespace(symbol="LTCUSDT", entry=100.0, sl=101.0, tp=102.0)
    token = _COST_CONTEXT.set(None)
    try:
        snapshot = cal._snapshot(sig)
    finally:
        _COST_CONTEXT.reset(token)

    assert snapshot["available"] is False
    assert snapshot["reason"] == "nexus_cost_context_unavailable"


def test_rr_calibration_is_scheduled_post_decision_and_not_awaited():
    source = inspect.getsource(validation_obs)
    assert "rr_gate_calibration.observe" in source
    assert "asyncio.create_task" in source
    assert "await rr_gate_calibration.observe" not in source


def test_rr_calibration_has_no_execution_or_threshold_mutation():
    source = inspect.getsource(cal)
    forbidden = (
        "place_order",
        "create_order",
        "cancel_order",
        "close_position",
        "_open(",
        "cfg.LEVERAGE =",
        "NEXUS_MIN_RR_NET =",
        "MIN_ENTRY_SCORE =",
        "MIN_VOLUME_MULT =",
    )
    assert all(token not in source for token in forbidden)
    assert "threshold_unchanged=true" in source
    assert "leverage_unchanged=true" in source
