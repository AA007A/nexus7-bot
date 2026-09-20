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
_LEASE_SECONDS = int(os.environ.get("EXECUTION_OWNERSHIP_LEASE_SECONDS","30"))

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
