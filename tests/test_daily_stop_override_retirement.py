from __future__ import annotations

from bot import durable_daily_stop


def test_persistent_daily_stop_override_is_not_authorization(monkeypatch):
    monkeypatch.setenv("DAILY_STOP_OPERATOR_OVERRIDE", "true")
    monkeypatch.delenv("DAILY_STOP_OVERRIDE_UTC_DAY", raising=False)

    assert durable_daily_stop._persistent_override_enabled() is True
    assert durable_daily_stop._operator_override_mode("2026-09-14") is None


def test_exact_day_override_remains_available(monkeypatch):
    monkeypatch.setenv("DAILY_STOP_OPERATOR_OVERRIDE", "true")
    monkeypatch.setenv("DAILY_STOP_OVERRIDE_UTC_DAY", "2026-09-14")

    assert durable_daily_stop._operator_override_mode("2026-09-14") == "date_scoped"
    assert durable_daily_stop._operator_override_mode("2026-09-15") is None


def test_persistent_override_requires_exact_true(monkeypatch):
    monkeypatch.setenv("DAILY_STOP_OPERATOR_OVERRIDE", "1")

    assert durable_daily_stop._persistent_override_enabled() is False
