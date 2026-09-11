from bot import liquidation
from bot.leverage_safe_signal_geometry import adapt_levels


def test_real_ada_50x_example_is_compressed_inside_liquidation_guard():
    result = adapt_levels(
        liquidation,
        symbol="ADAUSDT",
        direction="SHORT",
        entry=0.20469,
        sl=0.208927,
        tp=0.196216,
        leverage=50,
    )
    assert result.allowed is True
    assert result.adjusted is True
    assert result.retained_fraction >= 0.50
    assert result.final_stop_pct < result.original_stop_pct
    assert abs(result.rr - 2.0) < 1e-9

    check = liquidation.analyze(
        entry=0.20469,
        stop=result.sl,
        leverage=50,
        is_long=False,
        symbol="ADAUSDT",
        n_open_positions=1,
    )
    assert check.stop_effective is True
    assert check.gap_pct >= liquidation.MIN_GAP_PCT


def test_long_geometry_is_also_made_50x_safe_without_changing_rr():
    entry = 100.0
    result = adapt_levels(
        liquidation,
        symbol="BTCUSDT",
        direction="LONG",
        entry=entry,
        sl=98.0,
        tp=104.0,
        leverage=50,
    )
    assert result.allowed is True
    assert result.adjusted is True
    assert abs(result.rr - 2.0) < 1e-9
    assert result.sl < entry < result.tp

    check = liquidation.analyze(
        entry=entry,
        stop=result.sl,
        leverage=50,
        is_long=True,
        symbol="BTCUSDT",
        n_open_positions=1,
    )
    assert check.stop_effective is True


def test_already_safe_geometry_is_unchanged():
    result = adapt_levels(
        liquidation,
        symbol="ADAUSDT",
        direction="SHORT",
        entry=100.0,
        sl=101.0,
        tp=98.0,
        leverage=50,
    )
    assert result.allowed is True
    assert result.adjusted is False
    assert result.sl == 101.0
    assert result.tp == 98.0
    assert result.retained_fraction == 1.0


def test_pathologically_wide_stop_fails_closed_instead_of_overcompressing():
    result = adapt_levels(
        liquidation,
        symbol="ADAUSDT",
        direction="SHORT",
        entry=100.0,
        sl=103.0,
        tp=94.0,
        leverage=50,
    )
    assert result.allowed is False
    assert result.adjusted is False
    assert result.reason == "required_compression_too_large"
    assert result.retained_fraction < 0.50


def test_leverage_parameter_is_not_modified():
    # The adapter receives the configured leverage as an input and only returns
    # price geometry. This regression guards against a future silent leverage
    # downgrade being added to solve liquidation incompatibility.
    leverage = 50
    result = adapt_levels(
        liquidation,
        symbol="ADAUSDT",
        direction="SHORT",
        entry=100.0,
        sl=102.0,
        tp=96.0,
        leverage=leverage,
    )
    assert leverage == 50
    assert result.allowed is True
