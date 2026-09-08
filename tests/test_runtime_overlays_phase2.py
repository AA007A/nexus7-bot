import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_bootstrap_delegates_transitional_overlays():
    text = (ROOT / "bot" / "runtime_bootstrap.py").read_text(encoding="utf-8")
    assert "from bot import runtime_overlays as _runtime_overlays" in text
    assert "_runtime_overlays.install(TradingEngine, Analyzer, _log)" in text
    assert "TradingEngine._nexus_validate =" not in text
    assert "Analyzer.analyze_mtf =" not in text
    assert "_np._execute" not in text
    assert "asyncio.Lock" not in text


def test_runtime_overlays_preserve_remaining_idempotency_boundaries():
    text = (ROOT / "bot" / "runtime_overlays.py").read_text(encoding="utf-8")
    for marker in (
        "_mtf_shadow_patched",
        "_nexus_persistence_patched",
        "[RUNTIME_OVERLAYS] installed transitional overlays",
    ):
        assert marker in text


def test_persistence_serialization_is_not_monkey_patched_by_overlay():
    text = (ROOT / "bot" / "runtime_overlays.py").read_text(encoding="utf-8")
    assert "np._execute =" not in text
    assert "np._fetchall =" not in text
    assert "_single_conn_serialized" not in text
    assert "io_lock = asyncio.Lock()" not in text


def test_runtime_overlays_have_no_silent_except_handlers():
    path = ROOT / "bot" / "runtime_overlays.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    silent_lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                silent_lines.append(node.lineno)
    assert silent_lines == []


def test_runtime_overlays_have_no_direct_exchange_mutation_or_release_enablement():
    text = (ROOT / "bot" / "runtime_overlays.py").read_text(encoding="utf-8")
    for marker in (
        "PILOT_RELEASE_APPROVED=",
        "LIVE_TRADING_CONFIRMED=",
        "PAPER_TRADE=false",
        "place_order(",
        "cancel_order(",
        "cancel_all_orders(",
        "set_leverage(",
        "set_position_stops(",
        "close_position(",
        "._post(",
    ):
        assert marker not in text
