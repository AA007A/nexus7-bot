import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import runtime_contract_guard as guard


ROOT = Path(__file__).resolve().parents[1]


def _owned(module_name: str):
    def fn(*args, **kwargs):
        return None
    fn.__module__ = module_name
    return fn


def test_verify_accepts_expected_owner_modules():
    items = (
        guard.ContractItem("a", _owned("mod.a"), "mod.a"),
        guard.ContractItem("b", _owned("mod.b"), "mod.b"),
    )
    ok, errors = guard.verify(items)
    assert ok is True
    assert errors == ()


def test_verify_rejects_late_wrapper_drift():
    items = (
        guard.ContractItem("critical", _owned("unexpected.wrapper"), "reviewed.wrapper"),
    )
    ok, errors = guard.verify(items)
    assert ok is False
    assert len(errors) == 1
    assert "critical" in errors[0]
    assert "unexpected.wrapper" in errors[0]
    assert "reviewed.wrapper" in errors[0]


def test_install_fails_closed_when_final_runtime_owner_drifted(monkeypatch):
    class DummyLog:
        def critical(self, *args, **kwargs):
            return None

    class TradingEngine:
        run = _owned("wrong.module")
        _update_balance = _owned("bot.operator_runtime_policy")

    class PilotGuard:
        evaluate = _owned("bot.pilot_exposure_capacity")

    nexus_ai = SimpleNamespace(
        regime_compatibility=_owned("bot.nexus_regime_transition_consistency")
    )
    engine_module = SimpleNamespace(
        minimum_base_quantity=_owned("bot.operator_runtime_policy")
    )

    import bot.risk as risk
    import bot.risk_manager_v3 as risk_v3

    monkeypatch.setattr(risk.RiskManager, "can_open", _owned("bot.operator_runtime_policy"))
    monkeypatch.setattr(risk_v3.RiskManagerV3, "can_open", _owned("bot.operator_runtime_policy"))
    monkeypatch.delattr(builtins, "_nexus_runtime_contract_status", raising=False)

    with pytest.raises(RuntimeError, match="RUNTIME_CONTRACT_DRIFT"):
        guard.install(TradingEngine, PilotGuard, nexus_ai, engine_module, DummyLog())
    assert builtins._nexus_runtime_contract_status == "failed"


def test_runtime_contract_guard_is_last_installer_in_runtime_overlays():
    source = (ROOT / "bot" / "runtime_overlays.py").read_text(encoding="utf-8")
    guard_call = source.index("runtime_contract_guard.install(")
    # No additional .install(...) call may appear after the ownership contract.
    tail = source[guard_call + len("runtime_contract_guard.install("):]
    assert ".install(" not in tail


def test_guard_contract_explicitly_preserves_operator_policy():
    source = (ROOT / "bot" / "runtime_contract_guard.py").read_text(encoding="utf-8")
    assert '"bot.operator_runtime_policy"' in source
    assert "engine.minimum_base_quantity" in source
    assert "RiskManager.can_open" in source
    assert "RiskManagerV3.can_open" in source
    assert "entry_authorization_unchanged=true" in source
