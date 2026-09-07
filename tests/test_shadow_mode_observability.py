from pathlib import Path


def test_shadow_mode_observability_has_no_exchange_mutations():
    root = Path(__file__).resolve().parents[1]
    src = (root / "bot" / "shadow_mode_observability.py").read_text()
    forbidden = (
        "place_order(", "cancel_all_orders(", "set_leverage(", "set_sl(",
        "set_position_stops(", "close_position(", "._post(",
    )
    for token in forbidden:
        assert token not in src


def test_shadow_mode_reports_no_execution_effect():
    root = Path(__file__).resolve().parents[1]
    src = (root / "bot" / "shadow_mode_observability.py").read_text()
    assert 'out["trading_mode"] = "SHADOW_LIVE"' in src
    assert 'out["execution_effect"] = "NONE"' in src
    assert 'out["orders_sent_to_exchange"] = False' in src
    assert 'out["validation_lock"] = True' in src
