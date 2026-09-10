import logging
from unittest.mock import patch

from bot import logger as logger_module


def _record(message):
    return logging.LogRecord(
        name="test", level=logging.WARNING, pathname=__file__, lineno=1,
        msg=message, args=(), exc_info=None,
    )


def _reset():
    logger_module._BLOCK_LOG_LAST_KEY = ""
    logger_module._BLOCK_LOG_LAST_TS = 0.0


def test_identical_integrity_block_is_throttled_but_periodically_reemitted():
    _reset()
    filt = logger_module._RepeatedBlockFilter()
    msg = "🚫 ENTRADAS BLOQUEADAS: EXTERNAL_POSITION_UNPROTECTED: XRPUSDT"
    with patch.object(logger_module.time, "monotonic", side_effect=[100.0, 105.0, 161.0]):
        assert filt.filter(_record(msg)) is True
        assert filt.filter(_record(msg)) is False
        assert filt.filter(_record(msg)) is True


def test_changed_block_reason_is_emitted_immediately():
    _reset()
    filt = logger_module._RepeatedBlockFilter()
    first = "🚫 ENTRADAS BLOQUEADAS: EXTERNAL_POSITION_UNPROTECTED: XRPUSDT"
    changed = "🚫 ENTRADAS BLOQUEADAS: DAILY_STOP"
    with patch.object(logger_module.time, "monotonic", side_effect=[100.0, 101.0]):
        assert filt.filter(_record(first)) is True
        assert filt.filter(_record(changed)) is True


def test_non_integrity_logs_are_never_throttled():
    _reset()
    filt = logger_module._RepeatedBlockFilter()
    msg = "[SHADOW_CONNECT] stage=PING result=PASS execution_effect=NONE"
    assert filt.filter(_record(msg)) is True
    assert filt.filter(_record(msg)) is True
