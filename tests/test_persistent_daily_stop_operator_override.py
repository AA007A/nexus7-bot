import os
from datetime import datetime, timezone
from unittest.mock import patch

from bot import daily_stop_override_telemetry as telemetry
from bot import durable_daily_stop as daily_stop


class _Engine:
    paper_trade = False
    _validation_safety_lock_active = False
    daily_stopped = True
    daily_tracker = None


def _state():
    return {"trigger_pnl": -1.0, "stop_limit": 0.5}


def test_persistent_override_enabled_independent_of_utc_day():
    engine = _Engine()
    with patch.dict(
        os.environ,
        {
            "DAILY_STOP_OPERATOR_OVERRIDE": "true",
            "DAILY_STOP_OVERRIDE_UTC_DAY": "2026-09-14",
        },
        clear=False,
    ):
        assert daily_stop._operator_override_active(
            engine, "2026-09-15", state=_state()
        ) is True
        assert telemetry.override_mode_now() == "persistent"
    assert engine.daily_stopped is False


def test_persistent_override_is_explicit_true_only():
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    for value in ("", "false", "0", "yes", "TRUE-ish"):
        engine = _Engine()
        with patch.dict(
            os.environ,
            {
                "DAILY_STOP_OPERATOR_OVERRIDE": value,
                "DAILY_STOP_OVERRIDE_UTC_DAY": "2026-09-13",
            },
            clear=False,
        ):
            assert daily_stop._operator_override_active(
                engine, "2026-09-15", state=_state()
            ) is False
            assert telemetry.override_mode_now(now=now) is None
        assert engine.daily_stopped is True


def test_legacy_exact_day_override_remains_backward_compatible():
    engine = _Engine()
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    with patch.dict(
        os.environ,
        {
            "DAILY_STOP_OPERATOR_OVERRIDE": "false",
            "DAILY_STOP_OVERRIDE_UTC_DAY": "2026-09-15",
        },
        clear=False,
    ):
        assert daily_stop._operator_override_active(
            engine, "2026-09-15", state=_state()
        ) is True
        assert telemetry.override_mode_now(now=now) == "date_scoped"


def test_persistent_telemetry_rewrites_daily_stop_without_changing_pnl():
    original = (
        "🛑 *Stop-Loss DIÁRIO*\n"
        "PnL calculado (realizado + em aberto): `$-10.45`\n"
        "Novas entradas bloqueadas; posições abertas continuam sendo gerenciadas e protegidas."
    )
    with patch.dict(os.environ, {"DAILY_STOP_OPERATOR_OVERRIDE": "true"}, clear=False):
        rewritten, changed = telemetry.truthful_message(original)
    assert changed is True
    assert "$-10.45" in rewritten
    assert "Novas entradas permanecem liberadas" in rewritten
    assert "Novas entradas bloqueadas" not in rewritten


def test_override_does_not_change_durable_state_key():
    day = "2026-09-15"
    with patch.dict(os.environ, {"DAILY_STOP_OPERATOR_OVERRIDE": "true"}, clear=False):
        key_a = daily_stop.state_key(day)
    with patch.dict(os.environ, {"DAILY_STOP_OPERATOR_OVERRIDE": "false"}, clear=False):
        key_b = daily_stop.state_key(day)
    assert key_a == key_b
