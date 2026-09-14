from types import SimpleNamespace

from bot.runtime_mode_observability import (
    final_runtime_message,
    snapshot,
    startup_message,
)


def _engine(**kwargs):
    defaults = {
        "connected": False,
        "active": False,
        "_validation_safety_lock_active": False,
        "_shadow_prelive_readonly_ready": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_paper_never_reports_exchange_orders():
    state = snapshot(
        paper_trade=True,
        engine=_engine(connected=True, active=True),
        blocked=False,
        mode_reason="paper",
    )
    assert state["trading_mode"] == "PAPER"
    assert state["orders_sent_to_exchange"] is False
    assert state["ready"] is True


def test_shadow_live_never_reports_exchange_orders():
    engine = _engine(
        connected=True,
        active=True,
        _validation_safety_lock_active=True,
        _shadow_prelive_readonly_ready=True,
    )
    state = snapshot(paper_trade=False, engine=engine, blocked=False)
    assert state["trading_mode"] == "SHADOW_LIVE"
    assert state["orders_sent_to_exchange"] is False
    assert state["ready"] is True
    assert "SHADOW LIVE" in startup_message(state)
    assert "execution_effect=NONE" in startup_message(state)


def test_shadow_not_ready_until_readonly_pipeline_passes():
    engine = _engine(
        connected=True,
        active=True,
        _validation_safety_lock_active=True,
        _shadow_prelive_readonly_ready=False,
    )
    state = snapshot(paper_trade=False, engine=engine, blocked=False)
    assert state["ready"] is False
    assert state["orders_sent_to_exchange"] is False


def test_blocked_runtime_is_not_ready():
    state = snapshot(
        paper_trade=True,
        engine=_engine(connected=True, active=True),
        blocked=True,
    )
    assert state["ready"] is False
    assert state["blocked"] is True


def test_unlocked_live_reports_transitional_then_operational_state():
    not_ready = snapshot(
        paper_trade=False,
        engine=_engine(connected=False, active=False),
        blocked=False,
    )
    assert not_ready["trading_mode"] == "LIVE"
    assert not_ready["orders_sent_to_exchange"] is False
    banner = startup_message(not_ready)
    assert "NEXUS-7 INICIALIZANDO" in banner
    assert "Nenhuma falha de conexão é afirmada" in banner
    assert "engine desconectado" not in banner
    assert "engine inativo" not in banner

    ready = snapshot(
        paper_trade=False,
        engine=_engine(connected=True, active=True),
        blocked=False,
    )
    assert ready["orders_sent_to_exchange"] is True
    assert ready["ready"] is True
    ready_banner = startup_message(ready)
    assert "LIVE OPERACIONAL" in ready_banner


def test_live_startup_block_stays_transitional_not_false_disconnect():
    blocked = snapshot(
        paper_trade=False,
        engine=_engine(connected=False, active=False),
        blocked=True,
    )
    assert blocked["ready"] is False
    assert blocked["orders_sent_to_exchange"] is False
    banner = startup_message(blocked)
    assert "NEXUS-7 INICIALIZANDO" in banner
    assert "engine desconectado" not in banner


def test_final_runtime_message_reports_drawdown_gate(monkeypatch):
    from bot.config import cfg

    monkeypatch.setattr(cfg, "MAX_DRAWDOWN", 0.10)
    monkeypatch.delenv("LIVE_RISK_OVERRIDE_APPROVED", raising=False)
    engine = _engine(
        connected=True,
        active=True,
        paper_trade=False,
        risk=SimpleNamespace(drawdown=0.7218, balance=14.19),
        viable_symbols=["BTCUSDT"],
        pilot=None,
    )

    banner = final_runtime_message(engine)
    assert "LIVE BLOQUEADO" in banner
    assert "DRAWDOWN_HARD_GATE" in banner
    assert "72.18%" in banner
    assert "10.00%" in banner
