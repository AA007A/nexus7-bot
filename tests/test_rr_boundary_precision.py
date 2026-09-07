from bot.strategy import _rr_from_unrounded_levels


def test_rr_boundary_uses_unrounded_geometry():
    entry = 0.1000000
    risk = 0.0000017
    raw_sl = entry - risk
    raw_tp = entry + (risk * 2.0)

    # Six-decimal exchange rounding can materially distort cheap-coin R:R.
    rounded_rr = abs(round(raw_tp, 6) - entry) / abs(round(raw_sl, 6) - entry)
    assert rounded_rr < 2.0

    # The strategy gate must evaluate the intended geometry instead.
    rr = _rr_from_unrounded_levels(entry, raw_sl, raw_tp)
    assert abs(rr - 2.0) < 1e-9
    assert rr >= 2.0 - 1e-9


def test_rr_boundary_rejects_genuinely_subminimum_geometry():
    entry = 100.0
    raw_sl = 99.0
    raw_tp = 101.9
    assert _rr_from_unrounded_levels(entry, raw_sl, raw_tp) < 2.0
