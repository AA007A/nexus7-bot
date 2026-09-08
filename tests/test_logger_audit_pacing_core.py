import unittest
from pathlib import Path
from unittest.mock import patch

from bot import logger


class _FakeQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, value):
        self.items.append(value)


class LoggerAuditPacingCoreTests(unittest.TestCase):
    def setUp(self):
        logger._AUDIT_LAST_ENQUEUE = 0.0

    def tearDown(self):
        logger._AUDIT_LAST_ENQUEUE = 0.0

    def test_enqueue_preserves_one_point_five_second_pacing(self):
        fake_queue = _FakeQueue()
        with patch.object(logger, "_AI_TG_QUEUE", fake_queue), \
             patch.object(logger, "_ensure_tg_worker", lambda: None), \
             patch.object(logger.time, "monotonic", side_effect=[10.0, 10.5, 11.6]):
            logger._enqueue("first")
            logger._enqueue("too-soon")
            logger._enqueue("second")
        self.assertEqual(fake_queue.items, ["first", "second"])

    def test_sitecustomize_no_longer_patches_logger_enqueue(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "sitecustomize.py").read_text(encoding="utf-8")
        self.assertNotIn("_audit_pacing_patched", source)
        self.assertNotIn("_paced_enqueue", source)
        self.assertNotIn("_logger._enqueue", source)

    def test_core_logger_declares_pacing_state(self):
        self.assertEqual(logger._AUDIT_MIN_INTERVAL, 1.5)
        self.assertTrue(hasattr(logger, "_AUDIT_PACING_LOCK"))


if __name__ == "__main__":
    unittest.main()
