"""PostgreSQL-backed LIVE execution ownership with monotonic fencing.

This authority gates only mutations that can OPEN_NEW_RISK. Risk-reducing
reduceOnly/protection/reconciliation paths deliberately remain available.
"""
from __future__ import annotations
import os, uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from bot import database as db
from bot.critical_state import CriticalStateUnavailable
from bot.execution_capability import ExecutionCapability, current_execution_capability

_KEY = "live_execution_ownership_v1"
_OWNER_ID = os.environ.get("EXECUTION_OWNER_ID") or f"{os.environ.get('RAILWAY_SERVICE_ID','local')}:{uuid.uuid4().hex}"
_LEASE_SECONDS = int(os.environ.get("EXECUTION_OWNERSHIP_LEASE_SECONDS", str(15 * 2)))

class ExecutionOwnershipUnavailable(CriticalStateUnavailable): pass
class StaleExecutionFence(ExecutionOwnershipUnavailable): pass

@dataclass(frozen=True)
class ExecutionOwnership:
    owner_id: str
    fencing_token: int
    session_id: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str

def _session_id():
    return os.environ.get("RAILWAY_DEPLOYMENT_ID") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "local"

def _now(): return datetime.now(timezone.utc)
def _parse(v): return datetime.fromisoformat(v)

_ownership_observation_signature = None

def _local_expiry(ownership: ExecutionOwnership) -> datetime:
    """Normalize the persisted ISO lease deadline to the local readiness type."""
    expiry = _parse(ownership.expires_at)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry

def _observe_ownership_state(engine, *, event: str, db_lease_valid: bool | None,
                             fencing_valid: bool | None, reason: str) -> None:
    """Sanitized transition telemetry; never emits owner/session/fence identifiers."""
    global _ownership_observation_signature
    expiry = getattr(engine, "_execution_ownership_expires_at", None)
    local_lease_valid = isinstance(expiry, datetime) and expiry > _now()
    remaining = max(0.0, (expiry - _now()).total_seconds()) if isinstance(expiry, datetime) else 0.0
    execution_valid = bool(getattr(engine, "_execution_ownership_valid", False))
    signature = (event, db_lease_valid, local_lease_valid, execution_valid, fencing_valid, reason)
    if signature == _ownership_observation_signature:
        return
    _ownership_observation_signature = signature
    from bot.logger import log
    log.info(
        "[OWNERSHIP_STATE] event=%s db_lease_valid=%s local_lease_valid=%s "
        "execution_ownership_valid=%s fencing_valid=%s lease_remaining_seconds=%.3f reason=%s",
        event,
        str(db_lease_valid).lower() if db_lease_valid is not None else "unknown",
        str(local_lease_valid).lower(),
        str(execution_valid).lower(),
        str(fencing_valid).lower() if fencing_valid is not None else "unknown",
        remaining,
        reason,
    )

def observe_readiness_ownership_state(engine) -> None:
    _observe_ownership_state(
        engine,
        event="readiness_read",
        db_lease_valid=None,
        fencing_valid=None,
        reason="observe_only",
    )

def _publish_validated_ownership(engine, ownership: ExecutionOwnership, *, event: str) -> None:
    """Propagate a DB-validated lease into the canonical local readiness state."""
    engine._execution_ownership_expires_at = _local_expiry(ownership)
    engine._execution_ownership_valid = True
    _observe_ownership_state(
        engine,
        event=event,
        db_lease_valid=True,
        fencing_valid=True,
        reason="validated_lease_published",
    )

def _invalidate_local_ownership(engine, *, event: str, reason: str) -> None:
    engine._execution_ownership_valid = False
    engine._execution_ownership_expires_at = None
    _observe_ownership_state(
        engine,
        event=event,
        db_lease_valid=False,
        fencing_valid=False,
        reason=reason,
    )

async def acquire_execution_ownership(owner_id: str | None=None) -> ExecutionOwnership:
    if current_execution_capability() is not ExecutionCapability.LIVE:
        raise ExecutionOwnershipUnavailable("SHADOW_CANNOT_ACQUIRE_LIVE_EXECUTION_OWNERSHIP")
    conn=getattr(db,"_conn",None)
    if conn is None or not getattr(db,"_is_pg",False) or db.configured_postgres_unavailable():
        raise ExecutionOwnershipUnavailable("PostgreSQL ownership authority unavailable")
    owner=owner_id or _OWNER_ID; now=_now()
    async with db._io_lock:
      async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",_KEY)
        row=await conn.fetchrow("SELECT value FROM key_value WHERE key=$1 FOR UPDATE",_KEY)
        import json
        old=json.loads(row[0]) if row and row[0] else {}
        old_owner=str(old.get("owner_id","")); old_token=int(old.get("fencing_token",0) or 0)
        expires=old.get("expires_at"); live=False
        if expires:
            try: live=_parse(expires)>now
            except Exception: live=False
        if live and old_owner and old_owner!=owner:
            raise ExecutionOwnershipUnavailable("LIVE_EXECUTION_OWNERSHIP_HELD")
        token=old_token if live and old_owner==owner else old_token+1
        acquired=old.get("acquired_at") if live and old_owner==owner else now.isoformat()
        state=ExecutionOwnership(owner,token,_session_id(),acquired,now.isoformat(),(now+timedelta(seconds=_LEASE_SECONDS)).isoformat())
        payload=json.dumps(state.__dict__,sort_keys=True,separators=(",",":"))
        await conn.execute("INSERT INTO key_value(key,value,updated_at) VALUES($1,$2,$3) ON CONFLICT(key) DO UPDATE SET value=$2,updated_at=$3",_KEY,payload,now.isoformat())
        return state

async def validate_execution_ownership(ownership: ExecutionOwnership) -> None:
    if current_execution_capability() is not ExecutionCapability.LIVE:
        raise StaleExecutionFence("execution capability is not LIVE")
    conn=getattr(db,"_conn",None)
    if conn is None or not getattr(db,"_is_pg",False) or db.configured_postgres_unavailable():
        raise ExecutionOwnershipUnavailable("ownership validation database unavailable")
    import json
    async with db._io_lock:
        row=await conn.fetchrow("SELECT value FROM key_value WHERE key=$1",_KEY)
    if not row or not row[0]: raise StaleExecutionFence("REJECTED_STALE_FENCE missing authority")
    cur=json.loads(row[0]); now=_now()
    if str(cur.get("owner_id"))!=ownership.owner_id or int(cur.get("fencing_token",-1))!=ownership.fencing_token:
        raise StaleExecutionFence("REJECTED_STALE_FENCE superseded token")
    try: valid_until=_parse(str(cur.get("expires_at")))
    except Exception as exc: raise StaleExecutionFence("REJECTED_STALE_FENCE invalid lease") from exc
    if valid_until<=now: raise StaleExecutionFence("REJECTED_STALE_FENCE expired lease")


async def initialize_live_execution_ownership(engine):
    """Establish the LIVE lease during engine startup, before readiness can pass.

    READ_ONLY runtimes never acquire LIVE authority. The ownership object is
    stored on the raw exchange client because KuCoin's transport fence executes
    there, while the readiness bit lives on the engine.
    """
    if current_execution_capability() is not ExecutionCapability.LIVE:
        _invalidate_local_ownership(engine, event="local_invalidated", reason="capability_not_live")
        return None
    ownership = await acquire_execution_ownership()
    await validate_execution_ownership(ownership)
    client = getattr(engine, "client", None)
    raw_client = getattr(client, "_client", client)
    if raw_client is None:
        raise ExecutionOwnershipUnavailable("execution client unavailable")
    raw_client._execution_ownership = ownership
    # Keep proxy-visible state coherent for simple clients/tests.
    if client is not raw_client:
        client._execution_ownership = ownership
    _publish_validated_ownership(engine, ownership, event="startup_validated")
    from bot.logger import log
    log.info("[EXECUTION_OWNERSHIP] acquired=true fencing_valid=true lease_seconds=%s", _LEASE_SECONDS)
    return ownership


async def execution_ownership_heartbeat(engine):
    """Renew the LIVE lease for the current owner; fail closed on any loss."""
    import asyncio
    from bot.logger import log
    if current_execution_capability() is not ExecutionCapability.LIVE:
        _invalidate_local_ownership(engine, event="local_invalidated", reason="capability_not_live")
        return
    interval = max(1.0, _LEASE_SECONDS / 3.0)
    while getattr(engine, "_running", False):
        try:
            ownership = await acquire_execution_ownership()
            await validate_execution_ownership(ownership)
            client = getattr(engine, "client", None)
            raw_client = getattr(client, "_client", client)
            raw_client._execution_ownership = ownership
            if client is not raw_client:
                client._execution_ownership = ownership
            _publish_validated_ownership(engine, ownership, event="heartbeat_renewed")
            log.info("[EXECUTION_OWNERSHIP] heartbeat_renewed=true fencing_valid=true lease_seconds=%s", _LEASE_SECONDS)
        except Exception as exc:
            _invalidate_local_ownership(engine, event="heartbeat_failed", reason=type(exc).__name__)
            log.error("[EXECUTION_OWNERSHIP] heartbeat_invalid type=%s", type(exc).__name__)
        await asyncio.sleep(interval)
