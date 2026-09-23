"""Regression contract for BGX-PREDISPATCH-001."""
from __future__ import annotations

import inspect
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from bot.order_state import ManagedOrder, OrderState
from bot import pilot_submission_counter as psc


def _order() -> ManagedOrder:
    order = ManagedOrder("bgx7-test-predispatch", "ATOMUSDT", "Buy", 1.0)
    order.transition(OrderState.SUBMITTING, source="REST")
    return order


def test_missing_provenance_is_ambiguous():
    attempted, reason = psc._provenance(_order())
    assert attempted is None
    assert reason == ""


def test_authoritative_predispatch_abort_is_proven_not_dispatched():
    order = _order()
    psc._record_same_state(order, dispatch_attempted=False, predispatch_abort_reason="PRE_DISPATCH_PILOT_BUDGET_DENIED", exchange_dispatch="NONE")
    attempted, reason = psc._provenance(order)
    assert attempted is False
    assert reason == "PRE_DISPATCH_PILOT_BUDGET_DENIED"


def test_dispatch_attempted_is_monotonic_over_later_false_marker():
    order = _order()
    psc._record_same_state(order, dispatch_attempted=True)
    psc._record_same_state(order, dispatch_attempted=False, predispatch_abort_reason="SHOULD_NOT_OVERRIDE_TRUE")
    attempted, _ = psc._provenance(order)
    assert attempted is True


def test_submitting_can_terminalize_failed_without_fill_side_effect():
    order = _order()
    order.transition(OrderState.FAILED, source="PRE_DISPATCH_PILOT_BUDGET_DENIED")
    assert order.state == OrderState.FAILED
    assert order.is_terminal is True
    assert order.order_id is None
    assert order.filled_qty == 0
    assert order.avg_price == 0


def test_durable_budget_has_no_release_or_decrement_contract():
    source = inspect.getsource(psc)
    assert "tokens.append(token)" in source
    assert "tokens.remove(" not in source
    assert "tokens.pop(" not in source
    assert "release_submission" not in source


def test_two_token_cap_check_precedes_mutation():
    source = inspect.getsource(psc._reserve_db)
    assert source.index("if len(tokens) >= limit") < source.index("tokens.append(token)")


def test_existing_token_idempotency_precedes_cap():
    source = inspect.getsource(psc._reserve_db)
    assert source.index("if token in tokens") < source.index("if len(tokens) >= limit")


def test_all_deterministic_gates_precede_dispatch_marker_and_post():
    source = inspect.getsource(psc.install)
    ownership = source.index("await validate_execution_ownership(ownership)")
    transport_gate = source.index("post_context = self._entry_safe_post(")
    session_gate = source.index("session_committed = bool(commit(symbol, oid))")
    durable_gate = source.index("allowed, count = await reserve_submission(")
    marker = source.index("await _mark_dispatch_attempted(self, body)")
    post = source.index("async with post_context as response")
    assert ownership < transport_gate < session_gate < durable_gate < marker < post


def test_no_intentional_authorization_gate_after_dispatch_marker():
    source = inspect.getsource(psc.install)
    tail = source[source.index("await _mark_dispatch_attempted(self, body)"):]
    assert "reserve_submission(" not in tail
    assert "commit_submission" not in tail
    assert "validate_execution_ownership" not in tail
    assert "_entry_safe_post(" not in tail
    assert "async with post_context as response" in tail


def test_session_rejection_happens_before_durable_token_or_marker():
    source = inspect.getsource(psc.install)
    reject = source.index('raise RuntimeError("PILOT session submission cap reached at POST boundary")')
    durable = source.index("allowed, count = await reserve_submission(")
    marker = source.index("await _mark_dispatch_attempted(self, body)")
    assert reject < durable < marker


def test_durable_rejection_rolls_back_in_memory_before_failed_terminalization():
    source = inspect.getsource(psc.install)
    denied = source.index("if not allowed:")
    rollback = source.index("pilot.rollback_submission(oid)", denied)
    failed = source.index('"PRE_DISPATCH_PILOT_BUDGET_DENIED"', denied)
    marker = source.index("await _mark_dispatch_attempted(self, body)")
    assert denied < rollback < failed < marker


def test_in_memory_session_commit_is_at_transport_boundary():
    assert "commit_submission" in inspect.getsource(psc.install)
    assert "boundary=HTTP_POST" in inspect.getsource(psc._install_pilot_session_boundary)


def test_in_memory_commit_is_concurrency_safe_and_never_exceeds_two():
    from bot import pilot
    psc._install_pilot_session_boundary()
    guard = pilot.PilotGuard()
    with patch.object(pilot, "PILOT_ENABLED", True), patch.dict(os.environ, {"PAPER_TRADE": "false"}, clear=False):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda i: guard.commit_submission("ATOMUSDT", f"oid-{i}"), range(12)))
    assert sum(bool(x) for x in results) == 2
    assert guard.state.new_order_submissions_this_session == 2


def test_proven_not_dispatched_rollback_is_idempotent():
    from bot import pilot
    psc._install_pilot_session_boundary()
    guard = pilot.PilotGuard()
    with patch.object(pilot, "PILOT_ENABLED", True), patch.dict(os.environ, {"PAPER_TRADE": "false"}, clear=False):
        assert guard.commit_submission("ATOMUSDT", "oid-a") is True
        assert guard.state.new_order_submissions_this_session == 1
        assert guard.rollback_submission("oid-a") is True
        assert guard.rollback_submission("oid-a") is True
        assert guard.state.new_order_submissions_this_session == 0


def test_order_not_exist_is_not_used_as_terminalization_authority():
    source = inspect.getsource(psc)
    assert "orderNotExist" not in source
    assert "get_order_by_client_oid" not in inspect.getsource(psc._fail_predispatch)


def test_legacy_record_is_not_auto_adopted():
    attempted, abort_reason = psc._provenance(_order())
    assert attempted is None
    assert not abort_reason


def test_pilot_session_id_precedence_is_explicit():
    source = inspect.getsource(psc._session_id)
    assert source.index('os.environ.get("PILOT_SESSION_ID")') < source.index('os.environ.get("RAILWAY_DEPLOYMENT_ID")')
