import inspect

from bot import operator_runtime_policy as policy
from bot import risk_policy


def test_operator_margin_fraction_is_a_cap_constant():
    assert policy.MARGIN_FRACTION == risk_policy.DEFAULT_OPERATOR_MARGIN_CAP_PCT == 0.50


def test_operator_policy_never_mutates_leverage():
    source = inspect.getsource(policy)
    assert "cfg.LEVERAGE =" not in source


def test_operator_policy_no_longer_owns_quantity():
    assert not hasattr(policy, "_install_margin_sizing")
    source = inspect.getsource(policy)
    assert "authority=operator_margin_policy" not in source
    assert "return target_qty" not in source


def test_drawdown_policy_has_no_override_path_for_new_entries():
    source = inspect.getsource(policy._install_drawdown_advisory)
    protected = inspect.getsource(policy._protect_drawdown_update)
    assert "override_effect=NONE" in source
    assert "override=true entries_blocked=false" not in source
    assert "ALLOW_NEW_ENTRIES" not in source + protected
    assert "legacy_pause_preserved=true active_restored=false" in protected
    assert "self.active = True" not in protected
