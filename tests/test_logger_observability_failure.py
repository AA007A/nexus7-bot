import io
import logging
import unittest
from unittest.mock import patch

from bot import logger


class LoggerObservabilityFailureTests(unittest.TestCase):
    def setUp(self):
        logger._OBS_FAILURE_LAST.clear()

    def test_failure_is_reported_once_per_cooldown_without_raising(self):
        stream = io.StringIO()
        with patch.object(logger.sys, "stderr", stream), patch.object(
            logger.time, "monotonic", side_effect=[1000.0, 1001.0, 1301.0]
        ):
            logger._report_observability_failure("telegram_delivery", OSError("offline"))
            logger._report_observability_failure("telegram_delivery", OSError("offline"))
            logger._report_observability_failure("telegram_delivery", OSError("offline"))

        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("component=telegram_delivery" in line for line in lines))
        self.assertTrue(all("error=OSError" in line for line in lines))
        self.assertNotIn("offline", stream.getvalue())

    def test_handler_failure_remains_non_blocking_and_visible(self):
        handler = logger._DecisionTelegramHandler()
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "message", (), None
        )
        stream = io.StringIO()
        with patch.object(logger, "_parse_funnel", side_effect=ValueError("bad")), \
                patch.object(logger.sys, "stderr", stream):
            handler.emit(record)

        self.assertIn("component=decision_handler", stream.getvalue())
        self.assertIn("error=ValueError", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
