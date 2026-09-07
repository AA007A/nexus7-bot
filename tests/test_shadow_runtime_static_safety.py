from pathlib import Path


def test_shadow_runtime_fix_has_no_exchange_mutation_calls():
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "bot/shadow_integrity_isolation.py",
        "bot/shadow_startup_logging.py",
    ):
        src = (root / rel).read_text()
        for token in (
            "place_order(", "cancel_all_orders(", "set_leverage(",
            "set_sl(", "set_position_stops(", "close_position(", "._post(",
        ):
            assert token not in src
