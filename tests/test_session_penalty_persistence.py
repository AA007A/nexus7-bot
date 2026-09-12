import asyncio
import inspect
import json
import time

from bot import session_penalty_persistence as persistence
from bot import session_penalty_shadow as shadow


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args if args else message)

    def debug(self, message, *args):
        self.lines.append(message % args if args else message)


def _state(nexus_status="VETOED"):
    return {
        "symbol": "DOGEUSDT",
        "direction": "SHORT",
        "session": "ASIA",
        "penalty": -10,
        "base_score": 64,
        "adjusted_score": 54,
        "min_score": 60,
        "entry": 100.0,
        "sl": 101.0,
        "tp": 98.0,
        "opened_bar_ts": 1000,
        "last_bar_ts": 1000,
        "created_epoch": time.time(),
        "bars": 2,
        "mfe_pct": 0.4,
        "mae_pct": -0.2,
        "entry_type": "PULLBACK",
        "regime": "TRENDING_DOWN",
        "nexus_status": nexus_status,
        "nexus_approved": False if nexus_status != "APPROVED" else True,
        "nexus_reason": "test",
        "nexus_setup_quality": 61.0,
        "nexus_confidence": 55.0,
        "nexus_regime": "TRENDING_BEAR",
    }


def test_durable_upsert_is_idempotent_and_dedicated_to_shadow_table():
    original_exec = persistence.db._exec
    original_ready = persistence._TABLE_READY
    calls = []

    async def fake_exec(sql, params=(), **kwargs):
        calls.append((sql, params))
        return True

    async def scenario():
        persistence._TABLE_READY = False
        persistence.db._exec = fake_exec
        log = _Log()
        try:
            assert await persistence.save_state(
                "DOGEUSDT:SHORT:1000", _state(), status="OPEN", log=log
            ) is True
            assert await persistence.save_state(
                "DOGEUSDT:SHORT:1000", _state(), status="OPEN", log=log
            ) is True
        finally:
            persistence.db._exec = original_exec
            persistence._TABLE_READY = original_ready

    asyncio.run(scenario())
    sql = "\n".join(item[0] for item in calls)
    assert "session_penalty_shadow_audit" in sql
    assert "ON CONFLICT(state_key) DO UPDATE" in sql
    assert "opportunity_audit" not in sql


def test_restart_restores_only_open_recent_rows_and_fails_closed_pending_nexus():
    original_fetchall = persistence.db._fetchall
    original_ready = persistence._TABLE_READY
    captured = {}
    pending = _state("IN_PROGRESS")

    async def fake_fetchall(sql, params=(), **kwargs):
        captured["sql"] = sql
        captured["params"] = params
        return [("DOGEUSDT:SHORT:1000", json.dumps(pending))]

    async def scenario():
        persistence._TABLE_READY = True
        persistence.db._fetchall = fake_fetchall
        try:
            rows = await persistence.load_open(_Log())
        finally:
            persistence.db._fetchall = original_fetchall
            persistence._TABLE_READY = original_ready
        return rows

    rows = asyncio.run(scenario())
    assert "status='OPEN'" in captured["sql"]
    assert "updated_epoch>=?" in captured["sql"]
    assert len(rows) == 1
    key, state = rows[0]
    assert key == "DOGEUSDT:SHORT:1000"
    assert state["nexus_status"] == "INTERRUPTED_RESTART"
    assert state["nexus_approved"] is False
    assert "restart" in state["nexus_reason"]


def test_shadow_restore_rehydrates_active_cohort_without_execution_side_effect():
    original_load = persistence.load_open
    original_complete = shadow._RESTORE_COMPLETE
    original_scheduled = shadow._RESTORE_SCHEDULED
    log = _Log()

    async def fake_load(log):
        return [("DOGEUSDT:SHORT:1000", _state("VETOED"))]

    async def scenario():
        persistence.load_open = fake_load
        with shadow._LOCK:
            shadow._ACTIVE.clear()
            shadow._SEEN.clear()
            shadow._METRICS["restored"] = 0
        shadow._RESTORE_COMPLETE = False
        shadow._RESTORE_SCHEDULED = True
        try:
            await shadow._restore_persisted(log)
        finally:
            persistence.load_open = original_load
            shadow._RESTORE_COMPLETE = original_complete
            shadow._RESTORE_SCHEDULED = original_scheduled

    asyncio.run(scenario())
    with shadow._LOCK:
        assert "DOGEUSDT:SHORT:1000" in shadow._ACTIVE
        assert "DOGEUSDT:SHORT:1000" in shadow._SEEN
        assert shadow._ACTIVE["DOGEUSDT:SHORT:1000"]["bars"] == 2


def test_database_unavailable_is_observability_failure_only():
    original_exec = persistence.db._exec
    original_ready = persistence._TABLE_READY

    async def unavailable(sql, params=(), **kwargs):
        return False

    async def scenario():
        persistence._TABLE_READY = False
        persistence.db._exec = unavailable
        try:
            return await persistence.save_state(
                "DOGEUSDT:SHORT:1000", _state(), status="OPEN", log=_Log()
            )
        finally:
            persistence.db._exec = original_exec
            persistence._TABLE_READY = original_ready

    assert asyncio.run(scenario()) is False


def test_resolved_state_is_written_terminal_and_not_restored_by_query_contract():
    original_exec = persistence.db._exec
    original_ready = persistence._TABLE_READY
    calls = []

    async def fake_exec(sql, params=(), **kwargs):
        calls.append((sql, params))
        return True

    state = _state("APPROVED")
    state["outcome"] = "TP"
    state["net_pct"] = 1.5

    async def scenario():
        persistence._TABLE_READY = True
        persistence.db._exec = fake_exec
        try:
            return await persistence.save_state(
                "DOGEUSDT:SHORT:1000", state, status="RESOLVED", log=_Log()
            )
        finally:
            persistence.db._exec = original_exec
            persistence._TABLE_READY = original_ready

    assert asyncio.run(scenario()) is True
    insert = next((call for call in calls if "INSERT INTO session_penalty_shadow_audit" in call[0]), None)
    assert insert is not None
    assert "RESOLVED" in insert[1]
    source = inspect.getsource(persistence.load_open)
    assert "status='OPEN'" in source


def test_persistence_path_cannot_mutate_live_trading_policy_or_orders():
    source = inspect.getsource(persistence) + "\n" + inspect.getsource(shadow)
    forbidden = (
        "place_order", "create_order", "cancel_order", "close_position",
        "._open(", "cfg.LEVERAGE =", "MIN_ENTRY_SCORE =",
        "NEXUS_MIN_SCORE =", "MIN_VOLUME_MULT =", "_SESSION_PENALTY =",
        "TradingEngine.__init__ =",
    )
    assert all(token not in source for token in forbidden)
    assert "engine._nexus_validate(sig)" in source
    assert "decision_validation_error" in source
