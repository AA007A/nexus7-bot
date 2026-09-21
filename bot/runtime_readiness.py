"""Single semantic authority for permission to open new financial risk."""
from dataclasses import dataclass
from datetime import datetime, timezone
from bot.execution_capability import ExecutionCapability, current_execution_capability

@dataclass(frozen=True)
class RuntimeReadinessSnapshot:
    instruments_ready: bool
    critical_database_ready: bool
    financial_state_sane: bool
    initial_reconciliation_complete: bool
    execution_ownership_valid: bool
    exchange_ready: bool
    market_data_ready: bool
    execution_capability_live: bool
    protection_system_ready: bool
    @property
    def ready_for_new_entries(self):
        return all((self.instruments_ready,self.critical_database_ready,self.financial_state_sane,
          self.initial_reconciliation_complete,self.execution_ownership_valid,self.exchange_ready,
          self.market_data_ready,self.execution_capability_live,self.protection_system_ready))

def _ownership_locally_valid(engine) -> bool:
    if not bool(getattr(engine, "_execution_ownership_valid", False)):
        return False
    # Canonical TradingEngine instances carry an explicit lease deadline.
    # Legacy isolated fixtures without that field retain their boolean contract.
    if hasattr(engine, "_execution_ownership_expires_at"):
        expires_at = getattr(engine, "_execution_ownership_expires_at", None)
        if not isinstance(expires_at, datetime):
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at > datetime.now(timezone.utc)
    return True

def runtime_readiness(engine) -> RuntimeReadinessSnapshot:
    cap_live=current_execution_capability() is ExecutionCapability.LIVE
    ownership_valid = _ownership_locally_valid(engine)
    from bot.execution_ownership import observe_readiness_ownership
    observe_readiness_ownership(
        engine,
        ownership_valid,
        reason="valid_local_lease" if ownership_valid else "local_flag_or_lease_invalid",
    )
    return RuntimeReadinessSnapshot(
      instruments_ready=bool(getattr(engine,"instruments",{})),
      critical_database_ready=bool(getattr(engine,"_durable_state_ok",False)),
      financial_state_sane=bool(getattr(engine,"_financial_state_sane", getattr(getattr(engine,"risk",None),"balance_confirmed",False))),
      initial_reconciliation_complete=bool(getattr(engine,"_initial_reconciliation_complete", False)),
      execution_ownership_valid=ownership_valid,
      exchange_ready=bool(getattr(engine,"connected",False)),
      market_data_ready=bool(getattr(engine,"_market_data_ready", bool(getattr(engine,"viable_symbols",None)))),
      execution_capability_live=cap_live,
      protection_system_ready=bool(getattr(engine,"_protection_system_ready", False)),
    )

def assert_ready_for_new_entries(engine):
    snap=runtime_readiness(engine)
    if not snap.ready_for_new_entries:
        raise RuntimeError("READY_FOR_NEW_ENTRIES=false")
    return snap
