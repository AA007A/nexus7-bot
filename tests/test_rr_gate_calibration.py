import asyncio
import inspect
from types import SimpleNamespace

from bot import nexus_validation_observability as validation_obs
from bot import rr_gate_calibration as cal
from bot import nexus_live_cost_calibration as live_cost
from bot.missed_opportunity_audit import _signal_key as opportunity_signal_key
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


def _ctx():
    return NexusCostContext(
        symbol="LTCUSDT",
        taker_fee=0.0006,
        slippage=0.0003,
        fee_source="test_fee",
        slippage_source="test_slippage",
        spread_bps=2.0,
    )


def _sig():
    return SimpleNamespace(
        symbol="LTCUSDT",
        direction="LONG",
        entry_type="PULLBACK",
        entry=100.0,
        sl=101.0,
        tp=102.0,
    )


def test_snapshot_uses_in_scope_contextvar_without_io():
    sig = _sig()
    token = _COST_CONTEXT.set(_ctx())
    try:
        snapshot = cal._snapshot(sig)
    finally:
        _COST_CONTEXT.reset(token)

    assert snapshot["available"] is True
    assert snapshot["same_nexus_cost_context"] is True
    assert snapshot["cost_context_handoff"] == "contextvar_fallback"
    assert snapshot["rr_net_snapshot"] > 0
    assert snapshot["rr_net_threshold"] == 1.60
    assert snapshot["taker_fee_bps"] == 6.0
    assert snapshot["slippage_bps"] == 3.0
    assert snapshot["fee_source"] == "test_fee"
    assert snapshot["slippage_source"] == "test_slippage"


def test_snapshot_decomposes_exact_cost_adjusted_geometry():
    sig = _sig()
    decision = SimpleNamespace(_bgx_nexus_cost_context=_ctx())
    snapshot = cal._snapshot(sig, decision)

    # entry=100, stop distance=1%, target distance=2%; exact round-trip cost
    # is 2*(6bps+3bps)=18bps=0.18%.
    assert snapshot["geometry_available"] is True
    assert snapshot["stop_distance_pct"] == 1.0
    assert snapshot["target_distance_pct"] == 2.0
    assert snapshot["round_trip_cost_pct"] == 0.18
    assert snapshot["cost_to_stop_ratio"] == 0.18
    assert snapshot["gross_rr_snapshot"] == 2.0
    # Required gross gain for net RR 1.60:
    # 1.60*(1.00%+0.18%)+0.18%=2.068% => gross RR 2.068.
    assert snapshot["gross_rr_required_for_net_threshold"] == 2.068
    assert snapshot["required_target_distance_pct"] == 2.068
    assert snapshot["target_shortfall_pct"] == 0.068


def test_wider_stop_reduces_cost_load_for_same_gross_rr():
    ctx = _ctx()
    narrow = SimpleNamespace(entry=100.0, sl=99.5, tp=101.0)
    wide = SimpleNamespace(entry=100.0, sl=99.0, tp=102.0)
    n = cal._geometry_snapshot(narrow, ctx, 1.60)
    w = cal._geometry_snapshot(wide, ctx, 1.60)
    assert n["gross_rr_snapshot"] == 2.0
    assert w["gross_rr_snapshot"] == 2.0
    assert n["cost_to_stop_ratio"] > w["cost_to_stop_ratio"]
    assert n["gross_rr_required_for_net_threshold"] > w["gross_rr_required_for_net_threshold"]


def test_snapshot_uses_decision_handoff_after_contextvar_reset():
    sig = _sig()
    decision = SimpleNamespace(_bgx_nexus_cost_context=_ctx())
    token = _COST_CONTEXT.set(None)
    try:
        snapshot = cal._snapshot(sig, decision)
    finally:
        _COST_CONTEXT.reset(token)

    assert snapshot["available"] is True
    assert snapshot["same_nexus_cost_context"] is True
    assert snapshot["cost_context_handoff"] == "decision_handoff"
    assert snapshot["taker_fee_bps"] == 6.0
    assert snapshot["slippage_bps"] == 3.0
    assert snapshot["spread_bps"] == 2.0


def test_decision_handoff_wins_over_unrelated_contextvar():
    sig = _sig()
    decision = SimpleNamespace(_bgx_nexus_cost_context=_ctx())
    wrong = NexusCostContext(
        symbol="BTCUSDT",
        taker_fee=0.001,
        slippage=0.001,
        fee_source="wrong",
        slippage_source="wrong",
        spread_bps=9.0,
    )
    token = _COST_CONTEXT.set(wrong)
    try:
        snapshot = cal._snapshot(sig, decision)
    finally:
        _COST_CONTEXT.reset(token)

    assert snapshot["available"] is True
    assert snapshot["cost_context_handoff"] == "decision_handoff"
    assert snapshot["fee_source"] == "test_fee"
    assert snapshot["slippage_source"] == "test_slippage"


def test_snapshot_fails_closed_when_both_cost_sources_are_missing():
    sig = _sig()
    decision = SimpleNamespace()
    token = _COST_CONTEXT.set(None)
    try:
        snapshot = cal._snapshot(sig, decision)
    finally:
        _COST_CONTEXT.reset(token)

    assert snapshot["available"] is False
    assert snapshot["reason"] == "nexus_cost_context_unavailable"
    assert snapshot["cost_context_handoff"] == "unavailable"


def test_audit_signal_key_is_exactly_opportunity_audit_15m_cohort_key():
    sig = _sig()
    epoch = 1_789_178_730.0
    expected = opportunity_signal_key(sig.symbol, sig.direction, sig.entry_type, epoch)
    assert cal._audit_signal_key(sig, epoch) == expected


def test_current_row_queries_exact_signal_key_without_120_second_window():
    sig = _sig()
    fixed_now = 1_789_178_730.0
    expected_key = opportunity_signal_key(sig.symbol, sig.direction, sig.entry_type, fixed_now)
    captured = {}
    original_fetchall = cal.db._fetchall
    original_time = cal.time.time

    async def fake_fetchall(sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return [(expected_key, "{}", 0, "test")]

    cal.db._fetchall = fake_fetchall
    cal.time.time = lambda: fixed_now
    try:
        row = asyncio.run(cal._current_row(sig))
    finally:
        cal.db._fetchall = original_fetchall
        cal.time.time = original_time

    assert row[0] == expected_key
    assert captured["params"] == (expected_key,)
    assert "WHERE signal_key=?" in captured["sql"]
    assert "created_epoch>=?" not in captured["sql"]
    assert "120" not in inspect.getsource(cal._current_row)


def test_live_cost_wrapper_contains_private_decision_handoff_before_reset():
    source = inspect.getsource(live_cost)
    assert 'setattr(decision, "_bgx_nexus_cost_context", ctx)' in source
    assert "_COST_CONTEXT.reset(token)" in source
    assert source.index('setattr(decision, "_bgx_nexus_cost_context", ctx)') < source.index("_COST_CONTEXT.reset(token)")


def test_rr_calibration_is_scheduled_post_decision_and_not_awaited():
    source = inspect.getsource(validation_obs)
    assert "rr_gate_calibration.observe" in source
    assert "asyncio.create_task" in source
    assert "await rr_gate_calibration.observe" not in source
    assert "exact frozen candidate cost snapshot" in source


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
