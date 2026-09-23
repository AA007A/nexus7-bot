"""Durable fail-closed submission cap for the controlled LIVE pilot.

BGX-PREDISPATCH-001 adds durable dispatch provenance without changing the
cumulative two-token durable budget. The durable budget remains a submission
budget (not an active-order semaphore): tokens are never decremented here.

The in-memory PilotGuard reservation is committed only at the actual transport
boundary, after all final pre-dispatch gates have authorized a network POST.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from bot.logger import log
from bot.critical_state import critical_state

_LOCK = asyncio.Lock()
_STATE_VERSION = 1
_PROVENANCE_SOURCE = "BGX_PREDISPATCH_001"


def _session_id() -> str:
    """Return the configured pilot-session identity.

    PILOT_SESSION_ID intentionally has priority. Therefore when production
    defines it, the reset scope is operator/configuration session rather than
    process or Railway deployment.
    """
    raw = (
        os.environ.get("PILOT_SESSION_ID")
        or os.environ.get("RAILWAY_DEPLOYMENT_ID")
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or "local"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _state_key() -> str:
    return f"pilot_submission_budget_v1:{_session_id()}"


def _order_token(symbol: str, side: str, qty, idem_key: str | None) -> str:
    raw = f"{symbol}|{side}|{qty}|{idem_key or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _decode(value) -> list[str]:
    if value is None:
        return []
    try:
        obj = json.loads(str(value))
        if not isinstance(obj, dict) or obj.get("version") != _STATE_VERSION:
            raise ValueError("invalid durable pilot state")
        ids = obj.get("order_tokens")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise ValueError("invalid durable pilot order token list")
        return list(dict.fromkeys(ids))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("durable pilot state malformed") from exc


def _encode(tokens: list[str]) -> str:
    return json.dumps(
        {
            "version": _STATE_VERSION,
            "order_tokens": tokens,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def _reserve_db(token: str, limit: int) -> tuple[bool, int]:
    """Atomically reserve one cumulative unique submission token."""
    from bot import database as db

    critical_state.assert_available_for_new_risk()
    if limit <= 0:
        raise RuntimeError("invalid pilot submission limit")
    conn = getattr(db, "_conn", None)
    if conn is None:
        raise db.PersistenceError("pilot reservation blocked: database unavailable")
    if db.configured_postgres_unavailable():
        raise db.PersistenceError("pilot reservation blocked: PostgreSQL unavailable")

    key = _state_key()
    async with _LOCK:
        async with db._io_lock:
            if getattr(db, "_is_pg", False):
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
                    row = await conn.fetchrow(
                        "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", key
                    )
                    tokens = _decode(row[0] if row else None)
                    if token in tokens:
                        return True, len(tokens)
                    if len(tokens) >= limit:
                        return False, len(tokens)
                    tokens.append(token)
                    payload = _encode(tokens)
                    ts = datetime.now(timezone.utc).isoformat()
                    await conn.execute(
                        "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3) "
                        "ON CONFLICT (key) DO UPDATE SET value=$2,updated_at=$3",
                        key, payload, ts,
                    )
                    return True, len(tokens)

            await conn.execute("BEGIN IMMEDIATE")
            try:
                async with conn.execute(
                    "SELECT value FROM key_value WHERE key=?", (key,)
                ) as cur:
                    row = await cur.fetchone()
                tokens = _decode(row[0] if row else None)
                if token in tokens:
                    await conn.commit()
                    return True, len(tokens)
                if len(tokens) >= limit:
                    await conn.commit()
                    return False, len(tokens)
                tokens.append(token)
                payload = _encode(tokens)
                ts = datetime.now(timezone.utc).isoformat()
                await conn.execute(
                    "INSERT OR REPLACE INTO key_value (key,value,updated_at) VALUES (?,?,?)",
                    (key, payload, ts),
                )
                await conn.commit()
                return True, len(tokens)
            except Exception:
                await conn.rollback()
                raise


async def reserve_submission(
    symbol: str, side: str, qty, idem_key: str | None, limit: int
) -> tuple[bool, int]:
    token = _order_token(symbol, side, qty, idem_key)
    return await _reserve_db(token, limit)


def _provenance(order) -> tuple[bool | None, str]:
    """Read monotonic provenance from durable ManagedOrder history.

    None means legacy/missing authority and must remain fail-closed.
    """
    attempted = None
    abort_reason = ""
    for event in list(getattr(order, "history", []) or []):
        if not isinstance(event, (list, tuple)) or len(event) < 4:
            continue
        info = event[3]
        if not isinstance(info, dict):
            continue
        if info.get("dispatch_attempted") is True:
            attempted = True
        if attempted is not True and info.get("dispatch_attempted") is False:
            attempted = False
        reason = str(info.get("predispatch_abort_reason") or "")
        if reason:
            abort_reason = reason
    return attempted, abort_reason


def _record_same_state(order, **info) -> None:
    now = time.time()
    state = order.state.value
    payload = {"source": _PROVENANCE_SOURCE, **info}
    order.updated_at = now
    order.last_source = _PROVENANCE_SOURCE
    order.history.append((now, state, state, payload))


def _managed_order_for(client, symbol: str, side: str, qty, idem_key):
    registry = getattr(client, "_order_registry", None)
    if registry is None:
        return None
    try:
        oid = client.build_client_oid(symbol, side, qty, idem_key)
    except Exception:
        return None
    return registry.get(oid)


async def _persist_registry(client, reason: str, *, strict: bool = True) -> bool:
    engine = getattr(client, "_engine", None)
    if engine is None:
        return not strict
    from bot import durable_execution as durable
    return bool(await durable.persist_orders(engine, reason, strict=strict))


async def _fail_predispatch(
    client, symbol: str, side: str, qty, idem_key, reason: str
) -> bool:
    """Terminalize only when no mutation boundary has been crossed."""
    from bot.order_state import OrderState, InvalidTransition

    order = _managed_order_for(client, symbol, side, qty, idem_key)
    if order is None:
        return False
    attempted, _ = _provenance(order)
    if attempted is True:
        return False
    if order.is_terminal:
        return order.state == OrderState.FAILED

    _record_same_state(
        order,
        dispatch_attempted=False,
        predispatch_abort_reason=reason,
        exchange_dispatch="NONE",
    )
    try:
        order.transition(OrderState.FAILED, source=reason)
    except InvalidTransition:
        if order.state != OrderState.FAILED:
            raise
    await _persist_registry(client, "proven_not_dispatched_failed", strict=True)
    log.critical(
        "[PREDISPATCH_ABORT] clientOid=%s symbol=%s state=FAILED "
        "dispatch_attempted=false reason=%s",
        order.client_oid, symbol, reason,
    )
    return True


async def _mark_dispatch_attempted(client, body: dict) -> None:
    """Persist monotonic provenance immediately before aiohttp session.post."""
    if not isinstance(body, dict):
        return
    oid = str(body.get("clientOid") or "")
    if not oid.startswith("bgx7-"):
        return
    registry = getattr(client, "_order_registry", None)
    order = registry.get(oid) if registry is not None else None
    if order is None:
        raise RuntimeError("managed order missing at dispatch boundary")
    attempted, _ = _provenance(order)
    if attempted is True:
        return
    _record_same_state(order, dispatch_attempted=True, exchange_dispatch="ATTEMPTED")
    if not await _persist_registry(client, "dispatch_boundary", strict=True):
        raise RuntimeError("dispatch provenance persistence failed")
    log.info(
        "[DISPATCH_PROVENANCE] clientOid=%s symbol=%s dispatch_attempted=true",
        oid, order.symbol,
    )


def _install_pilot_session_boundary() -> None:
    """Make PilotGuard consumption occur only at the network mutation boundary."""
    from bot.pilot import PilotGuard, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION

    if getattr(PilotGuard, "_bgx_predispatch_session_boundary_installed", False):
        return

    def check_submission(self, symbol: str) -> bool:
        if not self.enabled:
            return True
        with self._submission_lock:
            count = self.state.new_order_submissions_this_session
            if count >= MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION:
                log.warning("[PILOT] %s submission cap reached (2)", symbol)
                return False
        log.debug(
            "[PILOT] symbol=%s submission_precheck=%s/%s session",
            symbol, count, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
        )
        return True

    def commit_submission(self, symbol: str, order_token: str) -> bool:
        if not self.enabled:
            return True
        with self._submission_lock:
            committed = getattr(self, "_bgx_committed_submission_tokens", None)
            if committed is None:
                committed = set()
                self._bgx_committed_submission_tokens = committed
            if order_token in committed:
                return True
            if self.state.new_order_submissions_this_session >= MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION:
                return False
            committed.add(order_token)
            self.state.new_order_submissions_this_session += 1
            if self.state.first_order_ts == 0.0:
                self.state.first_order_ts = time.time()
            count = self.state.new_order_submissions_this_session
        log.critical(
            "[PILOT] symbol=%s submission_committed=%s/%s session boundary=HTTP_POST",
            symbol, count, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
        )
        return True

    PilotGuard.reserve_submission = check_submission
    PilotGuard.commit_submission = commit_submission
    PilotGuard._bgx_predispatch_session_boundary_installed = True


def _install_reconciler_provenance() -> None:
    """Converge only explicitly proven pre-dispatch aborts.

    Legacy records with missing provenance and all dispatch-attempted records are
    delegated unchanged to the existing exchange-truth reconciler.
    """
    from bot import durable_live_reconciliation as reconciler
    from bot import durable_execution as durable
    from bot.order_state import OrderState, InvalidTransition

    if getattr(reconciler, "_bgx_predispatch_001_installed", False):
        return
    original = reconciler.reconcile_pending

    async def reconcile_pending_with_provenance(engine, *args, **kwargs):
        registry = getattr(engine, "orders", None)
        reader = getattr(registry, "pending_orders", None)
        changed = False
        if callable(reader):
            for order in list(reader() or []):
                attempted, abort_reason = _provenance(order)
                if attempted is False and abort_reason and not order.is_terminal:
                    try:
                        order.transition(OrderState.FAILED, source=abort_reason)
                    except InvalidTransition:
                        if order.state != OrderState.FAILED:
                            raise
                    changed = True
                    log.critical(
                        "[DURABLE_LIVE_RECONCILE] client_oid=%s symbol=%s "
                        "authority=PROVEN_NOT_DISPATCHED after=FAILED",
                        order.client_oid, order.symbol,
                    )
        if changed:
            if not await durable.persist_orders(
                engine, "predispatch_abort_reconcile", strict=True
            ):
                durable._block(engine, "orders")
                return False
        return await original(engine, *args, **kwargs)

    reconciler.reconcile_pending = reconcile_pending_with_provenance
    reconciler._bgx_predispatch_001_installed = True


def install(KuCoinClient, log_obj=log) -> None:
    """Install cumulative budget + exact dispatch-boundary lifecycle semantics."""
    if getattr(KuCoinClient, "_pilot_durable_submission_counter_installed", False):
        return

    _install_pilot_session_boundary()
    _install_reconciler_provenance()

    original_place_order = KuCoinClient.place_order
    original_fenced_entry_post = KuCoinClient._fenced_entry_post

    async def place_order_with_durable_pilot_budget(self, *args, **kwargs):
        reduce_only = bool(kwargs.get("reduce_only", False))
        single_submission = bool(kwargs.get("single_submission", False))
        if reduce_only or not single_submission:
            return await original_place_order(self, *args, **kwargs)

        from bot.pilot import MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION

        symbol = kwargs.get("symbol") or (args[0] if len(args) > 0 else "")
        side = kwargs.get("side") or (args[1] if len(args) > 1 else "")
        qty = kwargs.get("qty") if "qty" in kwargs else (args[2] if len(args) > 2 else 0)
        idem_key = kwargs.get("idem_key")
        try:
            allowed, count = await reserve_submission(
                str(symbol), str(side), qty, idem_key,
                MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
            )
        except Exception as exc:
            await _fail_predispatch(
                self, str(symbol), str(side), qty, idem_key,
                "PRE_DISPATCH_PILOT_BUDGET_ERROR",
            )
            log_obj.critical(
                "[PILOT_DURABLE_COUNTER] symbol=%s result=BLOCK error=%s "
                "exchange_dispatch=NONE",
                symbol, type(exc).__name__,
            )
            return {}

        if not allowed:
            await _fail_predispatch(
                self, str(symbol), str(side), qty, idem_key,
                "PRE_DISPATCH_PILOT_BUDGET_DENIED",
            )
            log_obj.critical(
                "[PILOT_DURABLE_COUNTER] symbol=%s result=BLOCK reserved=%s/%s "
                "exchange_dispatch=NONE",
                symbol, count, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
            )
            return {}

        log_obj.critical(
            "[PILOT_DURABLE_COUNTER] symbol=%s result=RESERVED reserved=%s/%s",
            symbol, count, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION,
        )
        try:
            result = await original_place_order(self, *args, **kwargs)
        except Exception:
            order = _managed_order_for(self, str(symbol), str(side), qty, idem_key)
            attempted, _ = _provenance(order) if order is not None else (None, "")
            if attempted is False or attempted is None:
                await _fail_predispatch(
                    self, str(symbol), str(side), qty, idem_key,
                    "PRE_DISPATCH_LOCAL_GATE_ABORT",
                )
            raise

        order = _managed_order_for(self, str(symbol), str(side), qty, idem_key)
        attempted, _ = _provenance(order) if order is not None else (None, "")
        if (not result or not result.get("orderId")) and attempted is not True:
            await _fail_predispatch(
                self, str(symbol), str(side), qty, idem_key,
                "PRE_DISPATCH_LOCAL_RETURN_NO_ORDER",
            )
        return result

    @asynccontextmanager
    async def fenced_entry_post_with_provenance(self, endpoint, body, url, **kwargs):
        is_new_risk = (
            endpoint in ("/api/v1/orders", "/api/v1/st-orders")
            and body.get("reduceOnly") is not True
            and body.get("closeOrder") is not True
        )
        if not is_new_risk:
            async with original_fenced_entry_post(
                self, endpoint, body, url, **kwargs
            ) as response:
                yield response
            return

        from bot.execution_ownership import validate_execution_ownership
        ownership = getattr(self, "_execution_ownership", None)
        if ownership is None:
            raise RuntimeError(
                "OPEN_NEW_RISK missing execution ownership at transport boundary"
            )
        await validate_execution_ownership(ownership)

        await _mark_dispatch_attempted(self, body)

        engine = getattr(self, "_engine", None)
        pilot = getattr(engine, "pilot", None) if engine is not None else None
        token = str(body.get("clientOid") or "")
        if pilot is not None:
            commit = getattr(pilot, "commit_submission", None)
            if callable(commit) and not commit(str(body.get("symbol") or ""), token):
                raise RuntimeError("PILOT session submission cap reached at POST boundary")

        async with self._entry_safe_post(
            endpoint, body, url, **kwargs
        ) as response:
            yield response

    KuCoinClient.place_order = place_order_with_durable_pilot_budget
    KuCoinClient._fenced_entry_post = fenced_entry_post_with_provenance
    KuCoinClient._pilot_durable_submission_counter_installed = True
    log_obj.info(
        "[PILOT_DURABLE_COUNTER] installed: cumulative two-token budget; "
        "BGX-PREDISPATCH-001 dispatch provenance and HTTP-boundary session accounting active"
    )
