import asyncio
from pathlib import Path
from unittest.mock import patch

from bot import nexus_persistence, nexus_zero_observability, notifier
from bot.nexus_validation_observability import observe_nexus_validation


ROOT = Path(__file__).resolve().parents[1]


class _Decision:
    execution_allowed = False

    def to_dict(self):
        return {"execution_allowed": False, "decision": "WAIT"}


class _Engine:
    client = object()


class _Sig:
    symbol = "BTCUSDT"


def test_decorator_returns_exact_decision_object():
    decision = _Decision()

    async def canonical(self, sig):
        return decision

    wrapped = observe_nexus_validation(canonical)

    async def run():
        with (
            patch.object(nexus_zero_observability, "observe"),
            patch.object(nexus_persistence, "record_decision", return_value=None),
            patch.object(nexus_persistence, "evaluate_pending", return_value=None),
            patch.object(notifier, "notify_nexus", return_value=None),
        ):
            out = await wrapped(_Engine(), _Sig())
            await asyncio.sleep(0)
        assert out is decision

    asyncio.run(run())


def test_persistence_failure_cannot_change_decision_result():
    decision = _Decision()

    async def canonical(self, sig):
        return decision

    async def fail_record(*args, **kwargs):
        raise RuntimeError("db telemetry unavailable")

    wrapped = observe_nexus_validation(canonical)

    async def run():
        with (
            patch.object(nexus_zero_observability, "observe"),
            patch.object(nexus_persistence, "record_decision", side_effect=fail_record),
            patch.object(notifier, "notify_nexus", return_value=None),
        ):
            out = await wrapped(_Engine(), _Sig())
            await asyncio.sleep(0)
        assert out is decision

    asyncio.run(run())


def test_canonical_exception_still_propagates_fail_closed():
    async def canonical(self, sig):
        raise ValueError("canonical failure")

    wrapped = observe_nexus_validation(canonical)

    async def run():
        try:
            await wrapped(_Engine(), _Sig())
        except ValueError as exc:
            assert str(exc) == "canonical failure"
        else:
            raise AssertionError("canonical exception was swallowed")

    asyncio.run(run())


def test_runtime_engine_uses_declarative_override_and_main_uses_runtime_class():
    runtime_text = (ROOT / "bot" / "nexus_runtime_engine.py").read_text(encoding="utf-8")
    main_text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "@observe_nexus_validation" in runtime_text
    assert "return await super()._nexus_validate(sig)" in runtime_text
    assert "from bot.nexus_runtime_engine import TradingEngine" in main_text


def test_observability_module_has_no_exchange_or_permission_mutation():
    text = (ROOT / "bot" / "nexus_validation_observability.py").read_text(encoding="utf-8")
    for marker in (
        "place_order(",
        "cancel_order(",
        "cancel_all_orders(",
        "set_leverage(",
        "set_position_stops(",
        "close_position(",
        "execution_allowed =",
        "PILOT_RELEASE_APPROVED=",
        "LIVE_TRADING_CONFIRMED=",
        "PAPER_TRADE=false",
    ):
        assert marker not in text
