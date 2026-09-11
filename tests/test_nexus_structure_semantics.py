from types import SimpleNamespace

from bot import nexus_structure_semantics as sem


def _model(direction="WAIT", confidence=0.0, structure="RANGING", bos=False,
           bos_dir="NONE", choch=False, available=True):
    return SimpleNamespace(
        name="STRUCTURE",
        available=available,
        direction=direction,
        confidence=confidence,
        details={
            "structure": structure,
            "bos": bos,
            "bos_dir": bos_dir,
            "choch": choch,
        },
    )


def _mtf(direction="LONG", score=100.0):
    return {"direction": direction, "score": score}


def test_aligned_directional_structure_keeps_canonical_confidence():
    score, reason = sem.contextual_structure_score(
        [_model(direction="LONG", confidence=70.0, structure="UPTREND")],
        _mtf(), "LONG", "TRENDING_BULL",
    )
    assert score == 70.0
    assert reason == "ALIGNED_DIRECTIONAL"


def test_opposite_directional_structure_never_gets_context_credit():
    score, reason = sem.contextual_structure_score(
        [_model(direction="SHORT", confidence=70.0, structure="DOWNTREND")],
        _mtf(), "LONG", "TRENDING_BULL",
    )
    assert score == 0.0
    assert reason == "OPPOSITE_DIRECTIONAL"


def test_ranging_structure_gets_small_credit_only_with_htf_support():
    score, reason = sem.contextual_structure_score(
        [_model(structure="RANGING")], _mtf(), "LONG", "TRENDING_BULL"
    )
    assert score == 25.0
    assert reason == "RANGING_NEUTRAL"

    no_mtf, _ = sem.contextual_structure_score(
        [_model(structure="RANGING")], _mtf(score=60), "LONG", "TRENDING_BULL"
    )
    assert no_mtf == 0.0

    no_regime, _ = sem.contextual_structure_score(
        [_model(structure="RANGING")], _mtf(), "LONG", "RANGE"
    )
    assert no_regime == 0.0


def test_accumulation_and_distribution_are_direction_specific():
    long_acc, _ = sem.contextual_structure_score(
        [_model(structure="ACCUMULATION")], _mtf(), "LONG", "TRENDING_BULL"
    )
    long_dist, _ = sem.contextual_structure_score(
        [_model(structure="DISTRIBUTION")], _mtf(), "LONG", "TRENDING_BULL"
    )
    short_dist, _ = sem.contextual_structure_score(
        [_model(structure="DISTRIBUTION")], _mtf("SHORT"), "SHORT", "TRENDING_BEAR"
    )
    assert long_acc == 35.0
    assert long_dist == 0.0
    assert short_dist == 35.0


def test_aligned_bos_from_neutral_state_gets_moderate_credit():
    score, reason = sem.contextual_structure_score(
        [_model(structure="RANGING", bos=True, bos_dir="BULLISH")],
        _mtf(), "LONG", "TRENDING_BULL",
    )
    assert score == 55.0
    assert reason == "RANGING_ALIGNED_BOS"


def test_choch_or_opposite_bos_remains_zero():
    choch_score, _ = sem.contextual_structure_score(
        [_model(structure="RANGING", choch=True)],
        _mtf(), "LONG", "TRENDING_BULL",
    )
    opposite_bos, _ = sem.contextual_structure_score(
        [_model(structure="RANGING", bos=True, bos_dir="BEARISH")],
        _mtf(), "LONG", "TRENDING_BULL",
    )
    assert choch_score == 0.0
    assert opposite_bos == 0.0


def test_recompute_preserves_neutral_and_unavailable_exclusions():
    weights = {
        "MARKET_STRUCTURE": 0.15,
        "TREND_ALIGNMENT": 0.15,
        "DERIVATIVES": 0.10,
        "MICROSTRUCTURE": 0.10,
    }
    sc = {
        "components": {
            "MARKET_STRUCTURE": 25.0,
            "TREND_ALIGNMENT": 80.0,
            "DERIVATIVES": 0.0,
            "MICROSTRUCTURE": 0.0,
        },
        "neutral": ["DERIVATIVES"],
        "unavailable": ["MICROSTRUCTURE"],
    }
    out = sem._recompute(sc, weights)
    # Only active weights: structure 0.15 + trend 0.15 = 0.30.
    assert out["weight_used"] == 0.30
    assert out["total"] == 52.5
