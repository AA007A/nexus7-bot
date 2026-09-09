import unittest

from bot import confidence_outcome_runtime as runtime


class _Log:
    def __init__(self):
        self.messages = []

    def info(self, *args, **kwargs):
        self.messages.append(("info", args))
        return None

    def warning(self, *args, **kwargs):
        self.messages.append(("warning", args))
        return None


class _DB:
    def __init__(self):
        self.open_calls = 0
        self.close_calls = 0
        self.rows = {7: (1.25,)}

    async def save_paper_open_atomic(
        self, symbol, side, entry, size, leverage, score, state_key,
        state_factory, strategy="MTF", score_features=None, sl=0,
        direction="",
    ):
        self.open_calls += 1
        return 7

    async def save_paper_close_atomic(
        self, trade_id, exit_price, pnl, fees, duration_min, exit_reason,
        state_key, state_value,
    ):
        self.close_calls += 1
        return True

    async def _fetchone(self, sql, params=()):
        return self.rows.get(int(params[0]))


class ConfidenceOutcomeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_direction_normalization(self):
        self.assertEqual(runtime._direction("Buy"), "LONG")
        self.assertEqual(runtime._direction("Sell"), "SHORT")
        self.assertEqual(runtime._direction("Buy", "SHORT"), "SHORT")

    async def test_paper_open_returns_original_trade_id_even_if_linker_fails(self):
        db = _DB()
        log = _Log()
        original_link = runtime._link_paper_open

        async def failing_link(*args, **kwargs):
            raise RuntimeError("telemetry failure")

        runtime._link_paper_open = failing_link
        try:
            runtime.install(db, log)
            trade_id = await db.save_paper_open_atomic(
                "BTCUSDT", "Buy", 100.0, 1.0, 5, 70,
                "state", lambda tid: str(tid), sl=98.0,
                direction="LONG",
            )
            self.assertEqual(trade_id, 7)
            self.assertEqual(db.open_calls, 1)
        finally:
            runtime._link_paper_open = original_link

    async def test_paper_close_returns_original_result(self):
        db = _DB()
        log = _Log()
        original_record = runtime._record_paper_close

        async def no_op_record(*args, **kwargs):
            return None

        runtime._record_paper_close = no_op_record
        try:
            runtime.install(db, log)
            result = await db.save_paper_close_atomic(
                7, 101.0, 1.0, 0.1, 5.0, "TP", "state", "value"
            )
            self.assertTrue(result)
            self.assertEqual(db.close_calls, 1)
        finally:
            runtime._record_paper_close = original_record

    async def test_successful_outcome_triggers_readonly_oos_readiness(self):
        db = _DB()
        log = _Log()
        original_record_outcome = runtime.record_trade_outcome
        original_readiness = runtime.maybe_log_readiness
        calls = []

        async def persisted_outcome(*args, **kwargs):
            return True

        async def readiness(db_arg, log_arg, **kwargs):
            calls.append((db_arg, log_arg, kwargs))
            return {
                "ready": False,
                "decision_effect": "NONE",
                "execution_effect": "NONE",
            }

        runtime.record_trade_outcome = persisted_outcome
        runtime.maybe_log_readiness = readiness
        try:
            await runtime._record_paper_close(db, trade_id=7, log=log)
            self.assertEqual(len(calls), 1)
            self.assertIs(calls[0][0], db)
            self.assertIs(calls[0][1], log)
        finally:
            runtime.record_trade_outcome = original_record_outcome
            runtime.maybe_log_readiness = original_readiness

    async def test_failed_outcome_persistence_does_not_trigger_oos_readiness(self):
        db = _DB()
        log = _Log()
        original_record_outcome = runtime.record_trade_outcome
        original_readiness = runtime.maybe_log_readiness
        calls = []

        async def rejected_outcome(*args, **kwargs):
            return False

        async def readiness(*args, **kwargs):
            calls.append(True)

        runtime.record_trade_outcome = rejected_outcome
        runtime.maybe_log_readiness = readiness
        try:
            await runtime._record_paper_close(db, trade_id=7, log=log)
            self.assertEqual(calls, [])
        finally:
            runtime.record_trade_outcome = original_record_outcome
            runtime.maybe_log_readiness = original_readiness

    async def test_oos_failure_does_not_relabel_persisted_outcome_as_link_failure(self):
        db = _DB()
        log = _Log()
        original_record_outcome = runtime.record_trade_outcome
        original_readiness = runtime.maybe_log_readiness

        async def persisted_outcome(*args, **kwargs):
            return True

        async def failing_readiness(*args, **kwargs):
            raise RuntimeError("analytics failure")

        runtime.record_trade_outcome = persisted_outcome
        runtime.maybe_log_readiness = failing_readiness
        try:
            await runtime._record_paper_close(db, trade_id=7, log=log)
            rendered = " ".join(str(args) for _, args in log.messages)
            self.assertIn("outcome_r=%s persisted=%s", rendered)
            self.assertIn("[NEXUS_OOS_CALIBRATION] trigger_failed", rendered)
            self.assertNotIn("close_link_failed", rendered)
        finally:
            runtime.record_trade_outcome = original_record_outcome
            runtime.maybe_log_readiness = original_readiness

    async def test_install_is_idempotent(self):
        db = _DB()
        log = _Log()
        runtime.install(db, log)
        first_open = db.save_paper_open_atomic
        runtime.install(db, log)
        self.assertIs(first_open, db.save_paper_open_atomic)


if __name__ == "__main__":
    unittest.main()
