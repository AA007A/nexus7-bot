import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bot import daily_stop_override_telemetry as telemetry


class DailyStopOverrideTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
        self.original = (
            "🛑 *Stop-Loss DIÁRIO*\n"
            "PnL calculado (realizado + em aberto): `$-10.45`\n"
            "Novas entradas bloqueadas; posições abertas continuam sendo gerenciadas e protegidas."
        )

    def test_matching_utc_day_annotates_that_override_was_ignored(self):
        with patch.dict(os.environ, {"DAILY_STOP_OVERRIDE_UTC_DAY": "2026-09-14"}, clear=False):
            text, changed = telemetry.truthful_message(self.original, now=self.now)

        self.assertTrue(changed)
        self.assertIn("IGNORADO", text)
        self.assertIn("$-10.45", text)
        self.assertIn("Novas entradas bloqueadas", text)
        self.assertNotIn("liberadas", text)

    def test_stale_override_preserves_blocked_message(self):
        with patch.dict(os.environ, {"DAILY_STOP_OVERRIDE_UTC_DAY": "2026-09-13"}, clear=False):
            text, changed = telemetry.truthful_message(self.original, now=self.now)

        self.assertFalse(changed)
        self.assertEqual(text, self.original)

    def test_malformed_override_never_activates(self):
        with patch.dict(os.environ, {"DAILY_STOP_OVERRIDE_UTC_DAY": "today"}, clear=False):
            self.assertFalse(telemetry.override_active_now(now=self.now))

    def test_non_daily_stop_notification_is_untouched(self):
        other = "🎯 *META DIÁRIA BATIDA!*"
        with patch.dict(os.environ, {"DAILY_STOP_OVERRIDE_UTC_DAY": "2026-09-14"}, clear=False):
            text, changed = telemetry.truthful_message(other, now=self.now)

        self.assertFalse(changed)
        self.assertEqual(text, other)

    def test_override_check_has_no_state_side_effects(self):
        with patch.dict(os.environ, {"DAILY_STOP_OVERRIDE_UTC_DAY": "2026-09-14"}, clear=False):
            before = dict(os.environ)
            self.assertTrue(telemetry.override_active_now(now=self.now))
            after = dict(os.environ)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
