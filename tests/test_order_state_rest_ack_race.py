from bot.order_state import InvalidTransition, ManagedOrder, OrderState


def _order() -> ManagedOrder:
    return ManagedOrder(
        client_oid="bgx7-rest-ack-race-test",
        symbol="LTCUSDT",
        side="Buy",
        qty=0.3,
    )


def test_late_rest_submitted_after_partial_fill_is_monotonic_noop():
    order = _order()
    order.transition(OrderState.SUBMITTING, source="ENGINE")
    order.transition(
        OrderState.PARTIALLY_FILLED,
        source="WS",
        filled_qty=0.1,
        avg_price=54.0,
    )
    updated_before = order.updated_at

    order.transition(
        OrderState.SUBMITTED,
        source="REST",
        order_id="488087622938263552",
        filled_qty=0.0,
        avg_price=1.0,
    )

    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.order_id == "488087622938263552"
    assert order.filled_qty == 0.1
    assert order.avg_price == 54.0
    assert order.last_source == "WS"
    assert order.updated_at == updated_before
    _, before, after, info = order.history[-1]
    assert before == OrderState.PARTIALLY_FILLED.value
    assert after == OrderState.PARTIALLY_FILLED.value
    assert info["monotonic_noop"] is True
    assert info["stale_ack_state"] == OrderState.SUBMITTED.value


def test_late_rest_submitted_after_filled_is_monotonic_noop():
    order = _order()
    order.transition(OrderState.SUBMITTING, source="ENGINE")
    order.transition(
        OrderState.FILLED,
        source="WS",
        filled_qty=0.3,
        avg_price=54.01,
    )

    order.transition(
        OrderState.SUBMITTED,
        source="REST",
        order_id="488087622938263552",
    )

    assert order.state is OrderState.FILLED
    assert order.order_id == "488087622938263552"
    assert order.filled_qty == 0.3
    assert order.avg_price == 54.01
    assert order.last_source == "WS"


def test_normal_submitting_to_submitted_is_unchanged():
    order = _order()
    order.transition(OrderState.SUBMITTING, source="ENGINE")
    order.transition(
        OrderState.SUBMITTED,
        source="REST",
        order_id="normal-order-id",
    )

    assert order.state is OrderState.SUBMITTED
    assert order.order_id == "normal-order-id"
    assert order.last_source == "REST"


def test_cancelled_to_submitted_remains_invalid():
    order = _order()
    order.transition(OrderState.SUBMITTING, source="ENGINE")
    order.transition(OrderState.SUBMITTED, source="REST", order_id="order-id")
    order.transition(OrderState.CANCELLED, source="WS")

    try:
        order.transition(OrderState.SUBMITTED, source="REST", order_id="order-id")
    except InvalidTransition:
        pass
    else:
        raise AssertionError("CANCELLED -> SUBMITTED must remain invalid")

    assert order.state is OrderState.CANCELLED


def test_failed_to_submitted_remains_invalid():
    order = _order()
    order.transition(OrderState.FAILED, source="ENGINE")

    try:
        order.transition(OrderState.SUBMITTED, source="REST", order_id="order-id")
    except InvalidTransition:
        pass
    else:
        raise AssertionError("FAILED -> SUBMITTED must remain invalid")

    assert order.state is OrderState.FAILED


def test_late_rest_ack_with_conflicting_order_id_fails_closed():
    order = _order()
    order.transition(OrderState.SUBMITTING, source="ENGINE")
    order.transition(
        OrderState.PARTIALLY_FILLED,
        source="WS",
        order_id="authoritative-order-id",
        filled_qty=0.1,
        avg_price=54.0,
    )

    try:
        order.transition(
            OrderState.SUBMITTED,
            source="REST",
            order_id="different-order-id",
        )
    except InvalidTransition:
        pass
    else:
        raise AssertionError("conflicting late REST order_id must fail closed")

    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.order_id == "authoritative-order-id"


def test_non_rest_backward_submitted_ack_remains_invalid():
    order = _order()
    order.transition(OrderState.SUBMITTING, source="ENGINE")
    order.transition(OrderState.PARTIALLY_FILLED, source="WS", filled_qty=0.1)

    try:
        order.transition(OrderState.SUBMITTED, source="WS")
    except InvalidTransition:
        pass
    else:
        raise AssertionError("only a stale REST ACK may be treated as monotonic no-op")

    assert order.state is OrderState.PARTIALLY_FILLED
