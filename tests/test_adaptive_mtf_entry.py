from bot.adaptive_mtf_entry import _gate


def _scores(s4=72, s1=70, s15=86, *, aligned=True, vol=1.8, adx=24, rsi=68):
    return (
        {"ok": True, "total": s4},
        {"ok": True, "total": s1},
        {
            "ok": True,
            "total": s15,
            "aligned": aligned,
            "vol_r": vol,
            "adx_v": adx,
            "rsi_v": rsi,
        },
    )


def test_long_fast_expansion_with_neutral_htf_can_pass():
    s4, s1, s15 = _scores()
    ok, reason = _gate(
        direction="LONG",
        regime="TRENDING_UP",
        bull_4h=False,
        bear_4h=False,
        bull_1h=True,
        bear_1h=False,
        s4h=s4,
        s1h=s1,
        s15=s15,
        combined=80,
        entry_type="MOMENTUM",
        extension_atr=1.2,
    )
    assert ok is True
    assert reason == "adaptive_expansion_confirmed"


def test_short_fast_expansion_with_neutral_4h_can_pass():
    s4, s1, s15 = _scores(rsi=38)
    ok, _ = _gate(
        direction="SHORT",
        regime="TRENDING_DOWN",
        bull_4h=False,
        bear_4h=False,
        bull_1h=False,
        bear_1h=True,
        s4h=s4,
        s1h=s1,
        s15=s15,
        combined=80,
        entry_type="BOS_BREAK",
        extension_atr=1.0,
    )
    assert ok is True


def test_explicit_opposite_higher_timeframe_stays_blocked():
    s4, s1, s15 = _scores()
    ok, reason = _gate(
        direction="LONG",
        regime="TRENDING_UP",
        bull_4h=False,
        bear_4h=True,
        bull_1h=True,
        bear_1h=False,
        s4h=s4,
        s1h=s1,
        s15=s15,
        combined=82,
        entry_type="MOMENTUM",
        extension_atr=1.0,
    )
    assert ok is False
    assert reason == "higher_timeframe_opposition"


def test_strict_alignment_is_left_to_canonical_strategy():
    s4, s1, s15 = _scores()
    ok, reason = _gate(
        direction="LONG",
        regime="TRENDING_UP",
        bull_4h=True,
        bear_4h=False,
        bull_1h=True,
        bear_1h=False,
        s4h=s4,
        s1h=s1,
        s15=s15,
        combined=82,
        entry_type="MOMENTUM",
        extension_atr=1.0,
    )
    assert ok is False
    assert reason == "canonical_alignment_already_present"


def test_pullback_is_not_rescued():
    s4, s1, s15 = _scores()
    ok, reason = _gate(
        direction="LONG",
        regime="TRENDING_UP",
        bull_4h=False,
        bear_4h=False,
        bull_1h=True,
        bear_1h=False,
        s4h=s4,
        s1h=s1,
        s15=s15,
        combined=80,
        entry_type="PULLBACK",
        extension_atr=1.0,
    )
    assert ok is False
    assert reason == "entry_not_expansion"


def test_weak_volume_is_blocked():
    s4, s1, s15 = _scores(vol=0.9)
    ok, reason = _gate(
        direction="LONG",
        regime="TRENDING_UP",
        bull_4h=False,
        bear_4h=False,
        bull_1h=True,
        bear_1h=False,
        s4h=s4,
        s1h=s1,
        s15m=s15 if False else s15,
        combined=80,
        entry_type="MOMENTUM",
        extension_atr=1.0,
    )
    assert ok is False
    assert reason == "volume_not_confirmed"


def test_overextended_price_is_blocked():
    s4, s1, s15 = _scores()
    ok, reason = _gate(
        direction="LONG",
        regime="TRENDING_UP",
        bull_4h=False,
        bear_4h=False,
        bull_1h=True,
        bear_1h=False,
        s4h=s4,
        s1h=s1,
        s15=s15,
        combined=80,
        entry_type="MOMENTUM",
        extension_atr=3.0,
    )
    assert ok is False
    assert reason == "price_too_extended"


def test_extreme_long_rsi_is_blocked():
    s4, s1, s15 = _scores(rsi=94)
    ok, reason = _gate(
        direction="LONG",
        regime="TRENDING_UP",
        bull_4h=False,
        bear_4h=False,
        bull_1h=True,
        bear_1h=False,
        s4h=s4,
        s1h=s1,
        s15=s15,
        combined=80,
        entry_type="BOS_BREAK",
        extension_atr=1.0,
    )
    assert ok is False
    assert reason == "long_rsi_overextended"
