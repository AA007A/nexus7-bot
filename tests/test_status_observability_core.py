from pathlib import Path
from unittest.mock import patch

from bot import funnel_metrics, mtf_shadow, nexus_decision_dedupe, nexus_persistence
from bot.status_observability import enrich_status


ROOT = Path(__file__).resolve().parents[1]


class _Risk:
    balance = 20.0
    drawdown = 0.125


class _Engine:
    paper_trade = True
    _paper_balance = 19.25
    risk = _Risk()


def test_status_enrichment_preserves_base_and_adds_metrics():
    with (
        patch.object(nexus_persistence, "get_cached_metrics", return_value={"total": 7}),
        patch.object(funnel_metrics, "get_funnel_metrics", return_value={"candidates": 3}),
        patch.object(mtf_shadow, "snapshot", return_value={"unique_states": 2}),
        patch.object(nexus_decision_dedupe, "snapshot", return_value={"deduped": 1}),
    ):
        base = {"connected": True, "balance": 20.0}
        out = enrich_status(_Engine(), base)

    assert base == {"connected": True, "balance": 20.0}
    assert out is not base
    assert out["nexus_persistent_metrics"] == {"total": 7}
    assert out["funnel_metrics"] == {"candidates": 3}
    assert out["mtf_shadow_metrics"] == {"unique_states": 2}
    assert out["nexus_dedupe_metrics"] == {"deduped": 1}
    assert out["paper_wallet"] == {
        "balance": 19.25,
        "drawdown_pct": 12.5,
        "isolated_from_exchange": True,
    }


def test_runtime_overlay_no_longer_patches_get_status():
    text = (ROOT / "bot" / "runtime_overlays.py").read_text(encoding="utf-8")
    assert "TradingEngine.get_status =" not in text
    assert "status_with_nexus_metrics" not in text
    assert "orig_status" not in text


def test_api_status_uses_explicit_enrichment():
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "from bot.status_observability import enrich_status" in text
    assert "enrich_status(app.state.engine, app.state.engine.get_status())" in text


def test_status_observability_has_no_execution_or_exchange_mutation():
    text = (ROOT / "bot" / "status_observability.py").read_text(encoding="utf-8")
    for marker in (
        "place_order(",
        "cancel_order(",
        "cancel_all_orders(",
        "set_leverage(",
        "set_position_stops(",
        "close_position(",
        "PILOT_RELEASE_APPROVED=",
        "LIVE_TRADING_CONFIRMED=",
        "PAPER_TRADE=false",
    ):
        assert marker not in text
