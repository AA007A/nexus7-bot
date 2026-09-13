import asyncio
from datetime import datetime, timedelta

from bot import post_trade_forensics as pf


class _Cfg:
    LEVERAGE = 50


class _Log:
    def __init__(self):
        self.rows = []

    def info(self, msg, *args):
        self.rows.append(("info", msg, args))

    def warning(self, msg, *args):
        self.rows.append(("warning", msg, args))


class _Position:
    def __init__(self):
        self.symbol = "AVAXUSDT"
        self.direction = "LONG"
        self.entry = 10.0
        self.current_price = 10.0
        self.qty = 2.0
        self.pnl = 0.0
        self.peak_pnl = 0.0
        self.score = 72
        self.sl = 9.8
        self.tp = 10.6
        self.opened_at = datetime.utcnow() - timedelta(minutes=20)

    def update_pnl(self, current_price):
        self.current_price = current_price
        self.pnl = (current_price - self.entry) * self.qty
        self.peak_pnl = max(self.peak_pnl, self.pnl)


class _Trade:
    symbol = "AVAXUSDT"
    entry = 10.0
    exit_price = 10.1
    qty = 2.0
    pnl_gross = 0.2
    total_fees = 0.02412
    pnl = pnl_gross - total_fees


class _Stats:
    def __init__(self):
        self.trades = []


class _Engine:
    async def _sync_positions(self):
        self.stats.trades.append(_Trade())
        self.positions.pop("AVAXUSDT", None)


def test_net_breakeven_includes_both_sides_fees():
    long_be = pf._net_breakeven(100.0, "LONG", 0.0006)
    short_be = pf._net_breakeven(100.0, "SHORT", 0.0006)
    assert long_be > 100.0
    assert short_be < 100.0
    assert round(long_be - 100.0, 4) == round(100.0 - short_be, 4)


def test_mfe_mae_tracking_and_close_report_are_telemetry_only():
    log = _Log()
    pf.install(_Engine, _Position, _Cfg, 0.0006, log)
    engine = _Engine()
    pos = _Position()
    engine.positions = {"AVAXUSDT": pos}
    engine.stats = _Stats()
    engine._last_nexus = {"AVAXUSDT": {"decision": "TRADE"}}

    pos.update_pnl(10.4)
    pos.update_pnl(9.9)
    pos.update_pnl(10.1)
    assert pos._forensic_mfe_pnl == 0.8
    assert pos._forensic_mae_pnl == -0.2

    asyncio.run(engine._sync_positions())

    warnings = [row for row in log.rows if row[0] == "warning"]
    assert len(warnings) == 1
    msg, args = warnings[0][1], warnings[0][2]
    assert "[POST_TRADE_FORENSICS]" in msg
    assert "decision_effect=NONE execution_effect=NONE" in msg
    assert args[0] == "AVAXUSDT"
    assert engine.positions == {}
    assert len(engine.stats.trades) == 1


def test_removed_without_new_trade_is_not_reported_as_completed_trade():
    class _NoTradeEngine:
        async def _sync_positions(self):
            self.positions.pop("AVAXUSDT", None)

    log = _Log()
    pf.install(_NoTradeEngine, _Position, _Cfg, 0.0006, log)
    engine = _NoTradeEngine()
    engine.positions = {"AVAXUSDT": _Position()}
    engine.stats = _Stats()
    engine._last_nexus = {}

    asyncio.run(engine._sync_positions())
    warnings = [row for row in log.rows if row[0] == "warning"]
    assert warnings == []
