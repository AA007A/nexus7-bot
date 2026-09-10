from types import SimpleNamespace

from bot.runtime_mode_observability import snapshot, startup_message


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


def test_unlocked_live_reports_orders_only_when_operational():
    not_ready = snapshot(
        paper_trade=False,
        engine=_engine(connected=False, active=False),
        blocked=False,
    )
    assert not_ready["trading_mode"] == "LIVE"
    assert not_ready["orders_sent_to_exchange"] is False

    ready = snapshot(
        paper_trade=False,
        engine=_engine(connected=True, active=True),
        blocked=False,
    )
    assert ready["orders_sent_to_exchange"] is True
    assert ready["ready"] is True
