import unittest
from types import SimpleNamespace

from bot import nexus_zero_observability as nz


class _Log:
    def __init__(self):
        self.info_calls = []
        self.debug_calls = []

    def info(self, *args):
        self.info_calls.append(args)

    def debug(self, *args):
        self.debug_calls.append(args)


def _decision(reason, score=0.0, confidence=0.0, allowed=False, dq=100.0):
    return SimpleNamespace(
        execution_allowed=allowed,
        setup_quality=score,
        confidence=confidence,
        reasoning=[reason],
        data_quality=dq,
        symbol="BTCUSDT",
    )


class NexusZeroObservabilityTests(unittest.TestCase):
    def test_zero_veto_is_classified_as_data_gate(self):
        log = _Log()
        nz.observe(_decision("Qualidade de dados 42/100 abaixo do mínimo (60)", dq=42), log)
        self.assertEqual(len(log.info_calls), 1)
        self.assertEqual(log.info_calls[0][2], "DATA_GATE")
        self.assertEqual(log.info_calls[0][3], 42.0)

    def test_zero_veto_is_classified_as_mtf_gate(self):
        log = _Log()
        nz.observe(_decision("Conflito entre timeframes (4H LONG / 1H SHORT) — seção 3"), log)
        self.assertEqual(len(log.info_calls), 1)
        self.assertEqual(log.info_calls[0][2], "MTF_GATE")

    def test_nonzero_or_approved_decisions_do_not_emit_zero_diagnostic(self):
        for decision in (
            _decision("score baixo", score=51.0, confidence=48.0),
            _decision("approved", score=60.0, confidence=55.0, allowed=True),
        ):
            log = _Log()
            nz.observe(decision, log)
            self.assertEqual(log.info_calls, [])


if __name__ == "__main__":
    unittest.main()
