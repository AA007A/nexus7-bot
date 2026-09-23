"""Regression contract for BGX-PREDISPATCH-001.

These tests are intentionally transport-free.  They pin the fail-closed
lifecycle invariants while the existing KuCoin/durable-counter suites continue
to cover database atomicity and exchange adapter behavior.
"""
from __future__ import annotations

import inspect

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
    psc._record_same_state(
        order,
        dispatch_attempted=False,
        predispatch_abort_reason="PRE_DISPATCH_PILOT_BUDGET_DENIED",
        exchange_dispatch="NONE",
    )
    attempted, reason = psc._provenance(order)
    assert attempted is False
    assert reason == "PRE_DISPATCH_PILOT_BUDGET_DENIED"


def test_dispatch_attempted_is_monotonic_over_later_false_marker():
    order = _order()
    psc._record_same_state(order, dispatch_attempted=True)
    psc._record_same_state(
        order,
        dispatch_attempted=False,
        predispatch_abort_reason="SHOULD_NOT_OVERRIDE_TRUE",
    )
    attempted, _ = psc._provenance(order)
    assert attempted is True


def test_submitting_can_terminalize_failed_without_fill_side_effect():
    order = _order()
    order.transition(
        OrderState.FAILED,
        source="PRE_DISPATCH_PILOT_BUDGET_DENIED",
    )
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
    cap = source.index("if len(tokens) >= limit")
    append = source.index("tokens.append(token)")
    assert cap < append


def test_existing_token_idempotency_precedes_cap():
    source = inspect.getsource(psc._reserve_db)
    existing = source.index("if token in tokens")
    cap = source.index("if len(tokens) >= limit")
    assert existing < cap


def test_dispatch_marker_is_before_actual_entry_safe_post():
    source = inspect.getsource(psc.install)
    marker = source.index("await _mark_dispatch_attempted(self, body)")
    network = source.index("async with self._entry_safe_post(")
    assert marker < network


def test_in_memory_session_commit_is_at_transport_boundary():
    source = inspect.getsource(psc.install)
    assert "commit_submission" in source
    assert "boundary=HTTP_POST" in inspect.getsource(psc._install_pilot_session_boundary)


def test_order_not_exist_is_not_used_as_terminalization_authority():
    source = inspect.getsource(psc)
    assert "orderNotExist" not in source
    assert "get_order_by_client_oid" not in inspect.getsource(psc._fail_predispatch)


def test_legacy_record_is_not_auto_adopted():
    order = _order()
    attempted, abort_reason = psc._provenance(order)
    assert attempted is None
    assert not abort_reason


def test_pilot_session_id_precedence_is_explicit():
    source = inspect.getsource(psc._session_id)
    assert source.index('os.environ.get("PILOT_SESSION_ID")') < source.index(
        'os.environ.get("RAILWAY_DEPLOYMENT_ID")'
    )
