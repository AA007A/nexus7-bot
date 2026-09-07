from pathlib import Path

from bot import shadow_state_isolation as isolation


class Engine:
    paper_trade = False
    _validation_safety_lock_active = True
    _durable_state_errors = {"state_divergence", "position_without_stop"}


def test_shadow_mode_requires_validation_lock():
    e = Engine()
    assert isolation.shadow_analysis_only(e) is True
    e._validation_safety_lock_active = False
    assert isolation.shadow_analysis_only(e) is False


def test_manual_position_divergence_is_not_a_shadow_analysis_blocker():
    assert isolation.durable_analysis_blocker(Engine()) == []


def test_database_and_order_faults_still_block_shadow_analysis():
    e = Engine()
    e._durable_state_errors = {"state_divergence", "database", "orders"}
    assert isolation.durable_analysis_blocker(e) == ["database", "orders"]


def test_shadow_files_contain_no_exchange_mutation_calls():
    root = Path(__file__).resolve().parents[1]
    source = (root / "bot" / "shadow_state_isolation.py").read_text()
    source += (root / "bot" / "shadow_live.py").read_text()
    forbidden = (
        "place_order(", "cancel_all_orders(", "set_leverage(", "set_sl(",
        "set_position_stops(", "close_position(", "._post(",
    )
    for token in forbidden:
        assert token not in source


def test_validation_lock_still_routes_live_open_only_to_shadow():
    root = Path(__file__).resolve().parents[1]
    source = (root / "bot" / "validation_safety_lock.py").read_text()
    assert "shadow_live.evaluate_candidate" in source
    assert "return await original_open" in source  # PAPER only path retained
