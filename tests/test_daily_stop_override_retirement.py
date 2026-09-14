from __future__ import annotations

import os
from unittest.mock import patch

from bot import durable_daily_stop


def test_persistent_daily_stop_override_is_not_authorization():
    with patch.dict(
        os.environ,
        {
            "DAILY_STOP_OPERATOR_OVERRIDE": "true",
            "DAILY_STOP_OVERRIDE_UTC_DAY": "",
        },
        clear=False,
    ):
        assert durable_daily_stop._persistent_override_enabled() is True
        assert durable_daily_stop._operator_override_mode("2026-09-14") is None


def test_exact_day_override_remains_available():
    with patch.dict(
        os.environ,
        {
            "DAILY_STOP_OPERATOR_OVERRIDE": "true",
            "DAILY_STOP_OVERRIDE_UTC_DAY": "2026-09-14",
        },
        clear=False,
    ):
        assert durable_daily_stop._operator_override_mode("2026-09-14") == "date_scoped"
        assert durable_daily_stop._operator_override_mode("2026-09-15") is None


def test_persistent_override_requires_exact_true():
    with patch.dict(os.environ, {"DAILY_STOP_OPERATOR_OVERRIDE": "1"}, clear=False):
        assert durable_daily_stop._persistent_override_enabled() is False
