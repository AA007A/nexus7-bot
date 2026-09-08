import unittest

from bot import confidence_outcome_runtime as runtime


class _Log:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
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

    async def test_install_is_idempotent(self):
        db = _DB()
        log = _Log()
        runtime.install(db, log)
        first_open = db.save_paper_open_atomic
        runtime.install(db, log)
        self.assertIs(first_open, db.save_paper_open_atomic)


if __name__ == "__main__":
    unittest.main()
