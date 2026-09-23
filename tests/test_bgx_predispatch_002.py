"""BGX-PREDISPATCH-002: legacy dispatch ambiguity remains fail-closed."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from bot import durable_reconcile_hardening as hardening
from bot import pilot_submission_counter as psc
from bot.durable_live_reconciliation import apply_exchange_order_truth
from bot.order_state import ManagedOrder, OrderRegistry, OrderState


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Client:
    def __init__(self, by_oid=None, positions=None, active=None, error=None):
        self.by_oid = by_oid
        self.positions = [] if positions is None else positions
        self.active = {"items": []} if active is None else active
        self.error = error

    async def get_order_by_client_oid(self, _oid):
        if self.error:
            raise self.error
        return self.by_oid

    async def get_positions(self):
        if self.error:
            raise self.error
        return self.positions

    async def _get(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.active


class _Durable:
    def __init__(self):
        self.blocked = False
        self.persisted = 0
        self.reconcile_orders = self._original

    async def _original(self, _engine):
        return False

    def _block(self, _engine, _reason):
        self.blocked = True

    def _clear(self, _engine, _reason):
        self.blocked = False

    async def persist_orders(self, _engine, _reason, strict=False):
        self.persisted += 1
        return True

    @staticmethod
    def _advance(order, state, **info):
        if state == OrderState.FILLED and order.state == OrderState.SUBMITTING:
            order.transition(OrderState.SUBMITTED, **info)
        order.transition(state, **info)


def _order(state=OrderState.SUBMITTING, age=3600.0):
    order = ManagedOrder("bgx7-test-legacy-authority", "ATOMUSDT", "Buy", 2.0)
    if state == OrderState.SUBMITTING:
        order.transition(OrderState.SUBMITTING, source="TEST")
    elif state != OrderState.CREATED:
        raise AssertionError("unsupported fixture state")
    order.created_at = time.time() - age
    return order


def _engine(order, client=None):
    registry = OrderRegistry()
    registry._orders[order.client_oid] = order
    return SimpleNamespace(orders=registry, client=client or _Client())


def _run_startup(order, client=None):
    durable = _Durable()
    hardening.install(durable, SimpleNamespace(OrderState=OrderState), _Log())
    engine = _engine(order, client)
    result = asyncio.run(durable.reconcile_orders(engine))
    return result, durable, engine


def _mark_proven_not_dispatched(order):
    psc._record_same_state(
        order,
        dispatch_attempted=False,
        predispatch_abort_reason="TEST_PRE_DISPATCH_DENIAL",
        exchange_dispatch="NONE",
    )


def test_a_proven_not_dispatched_submitting_can_fail():
    order = _order()
    _mark_proven_not_dispatched(order)
    result, durable, _ = _run_startup(order)
    assert result is True
    assert order.state == OrderState.FAILED
    assert durable.blocked is False


def test_b_legacy_submitting_absent_flat_no_active_remains_unresolved():
    order = _order(age=301)
    result, durable, engine = _run_startup(order)
    assert result is False
    assert order.state == OrderState.SUBMITTING
    assert len(engine.orders.pending_orders()) == 1
    assert durable.blocked is True


def test_c_extremely_old_legacy_submitting_still_ambiguous():
    order = _order(age=365 * 86400)
    result, durable, _ = _run_startup(order)
    assert result is False
    assert order.state == OrderState.SUBMITTING
    assert durable.blocked is True


def test_d_legacy_created_without_provenance_remains_nonterminal():
    order = _order(OrderState.CREATED, age=3600)
    result, durable, engine = _run_startup(order)
    assert result is False
    assert order.state == OrderState.CREATED
    assert len(engine.orders.pending_orders()) == 1
    assert durable.blocked is True


def test_e_created_with_proven_not_dispatched_can_fail():
    order = _order(OrderState.CREATED)
    _mark_proven_not_dispatched(order)
    result, _, _ = _run_startup(order)
    assert result is True
    assert order.state == OrderState.FAILED


def test_f_g_h_authoritative_exchange_truth_preserved():
    active = _order(age=0)
    changed, terminal = apply_exchange_order_truth(
        SimpleNamespace(orders=OrderRegistry()), active,
        {"clientOid": active.client_oid, "orderId": "o1", "isActive": True, "filledSize": 0},
        source="TEST",
    )
    assert changed and not terminal and active.state == OrderState.SUBMITTED

    filled = _order(age=0)
    changed, terminal = apply_exchange_order_truth(
        SimpleNamespace(orders=OrderRegistry()), filled,
        {"clientOid": filled.client_oid, "orderId": "o2", "isActive": False, "status": "done", "filledSize": 2},
        source="TEST",
    )
    assert changed and terminal and filled.state == OrderState.FILLED

    rejected = _order(age=0)
    changed, terminal = apply_exchange_order_truth(
        SimpleNamespace(orders=OrderRegistry()), rejected,
        {"clientOid": rejected.client_oid, "orderId": "o3", "isActive": False, "status": "rejected", "filledSize": 0},
        source="TEST",
    )
    assert changed and terminal and rejected.state == OrderState.REJECTED


def test_i_partial_fill_terminal_cancel_preserves_filled_qty():
    order = _order(age=0)
    order.transition(OrderState.SUBMITTED, order_id="o4", source="TEST")
    order.transition(OrderState.PARTIALLY_FILLED, order_id="o4", filled_qty=0.5, source="TEST")
    changed, terminal = apply_exchange_order_truth(
        SimpleNamespace(orders=OrderRegistry()), order,
        {"clientOid": order.client_oid, "orderId": "o4", "isActive": False, "status": "cancelled", "cancelExist": True, "filledSize": 0.75},
        source="TEST",
    )
    assert changed and terminal
    assert order.state == OrderState.CANCELLED
    assert order.filled_qty == 0.75


def test_j_k_order_absence_never_terminalizes_ambiguous_intent():
    for age in (0, 301, 999999):
        order = _order(age=age)
        result, durable, _ = _run_startup(order, _Client(by_oid=None, positions=[], active={"items": []}))
        assert result is False
        assert order.state == OrderState.SUBMITTING
        assert durable.blocked is True


def test_l_m_malformed_or_exception_remains_fail_closed():
    malformed = _order()
    result, durable, _ = _run_startup(malformed, _Client(active="malformed"))
    assert result is False and malformed.state == OrderState.SUBMITTING and durable.blocked

    errored = _order()
    result, durable, _ = _run_startup(errored, _Client(error=RuntimeError("read failed")))
    assert result is False and errored.state == OrderState.SUBMITTING and durable.blocked


def test_n_repeated_restart_reconciliation_is_idempotently_fail_closed():
    order = _order()
    first, _, _ = _run_startup(order)
    second, _, _ = _run_startup(order)
    assert first is False and second is False
    assert order.state == OrderState.SUBMITTING


def test_o_continuous_and_startup_absence_authority_are_compatible():
    import inspect
    from bot import durable_live_reconciliation as live
    startup_source = inspect.getsource(hardening.install)
    live_source = inspect.getsource(live.reconcile_pending)
    assert "historical_not_dispatched_proven=false" in startup_source
    assert "if not data:" in live_source
    assert "unresolved.append(order.client_oid)" in live_source


def test_p_q_no_pilot_or_financial_side_effect_added():
    import inspect
    source = inspect.getsource(hardening)
    assert "rollback_submission" not in source
    assert "release_submission" not in source
    assert "PnL" not in source
    assert "create_trade" not in source
    assert "place_order" not in source
    assert "cancel_order" not in source


def test_r_s_t_unresolved_ambiguity_keeps_pending_blocked_and_not_ready():
    order = _order()
    result, durable, engine = _run_startup(order)
    assert result is False
    assert len(engine.orders.pending_orders()) > 0
    assert durable.blocked is True
