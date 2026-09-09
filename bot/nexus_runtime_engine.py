"""Explicit runtime TradingEngine composition for NEXUS-7.

The large canonical engine remains in ``bot.engine``. This runtime subclass
adds post-decision observability declaratively and prepares professional,
stop-aware risk state for the executable core without replacing engine methods
at startup.

No exchange mutation, release state, or execution permission is changed here.
"""
from __future__ import annotations

from bot.account_capital_reader import read_account_capital
from bot.core_execution_risk import final_read_only_dispatch_recheck
from bot.engine import TradingEngine as CoreTradingEngine
from bot.logger import log
from bot.nexus_validation_observability import observe_nexus_validation
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter


class TradingEngine(CoreTradingEngine):
    """Canonical engine plus explicit observability and RiskManagerV3 sizing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not isinstance(self.risk, ProfessionalRiskAdapter):
            self.risk = ProfessionalRiskAdapter(self.risk)
        self._professional_risk_candidate_symbol = ""

    async def _prepare_professional_risk(self, sig, decision) -> None:
        """Prepare one fail-closed sizing plan after an AI approval.

        SHADOW LIVE already has its own fully read-only RiskManagerV3 pipeline,
        so the executable-core adapter intentionally stays dormant while the
        validation lock is active.
        """
        if getattr(self, "_validation_safety_lock_active", False):
            return
        if getattr(decision, "execution_allowed", None) is not True:
            return

        risk_pct = float(self._effective_risk_pct())
        self.risk.set_plan(
            symbol=sig.symbol,
            entry=float(sig.entry),
            stop=float(sig.sl),
            risk_pct=risk_pct,
        )
        self._professional_risk_candidate_symbol = str(sig.symbol)

        if getattr(self, "paper_trade", False):
            balance = float(getattr(self.risk, "balance", 0.0) or 0.0)
            if balance <= 0:
                self.risk.invalidate_capital()
                raise RuntimeError("PAPER capital unavailable for RiskManagerV3")
            self.risk.update_capital(CapitalState(
                equity=balance,
                available_collateral=balance,
            ))
            return

        try:
            snapshot = await read_account_capital(self.client)
            self.risk.update_capital(snapshot.capital)
        except Exception:
            self.risk.invalidate_capital()
            raise

    async def _refresh_entry_balance(self) -> bool:
        """Refresh funds, then fail closed on last-moment exchange exposure.

        The canonical ``_open`` calls this immediately before it creates the
        OrderRegistry intent and persists ``before_dispatch``. PAPER keeps the
        canonical balance-only path. SHADOW LIVE never uses the executable core
        dispatch path and therefore keeps its existing dedicated read-only gate.
        """
        refreshed = await super()._refresh_entry_balance()
        if not refreshed:
            return False
        if getattr(self, "paper_trade", False):
            return True
        if getattr(self, "_validation_safety_lock_active", False):
            return True

        symbol = str(getattr(self, "_professional_risk_candidate_symbol", ""))
        if not symbol:
            log.critical(
                "[CORE_FINAL_EXPOSURE] result=BLOCK reason=CANDIDATE_SYMBOL_MISSING"
            )
            return False

        result = await final_read_only_dispatch_recheck(self.client, symbol)
        blockers = tuple(result.blockers or ())
        if not result.allowed:
            log.critical(
                "[CORE_FINAL_EXPOSURE] symbol=%s result=BLOCK blockers=%s "
                "positions=%s active_orders=%s",
                symbol,
                ",".join(blockers) or "UNKNOWN",
                result.metrics.get("active_positions", "NA"),
                result.metrics.get("active_orders", "NA"),
            )
            return False

        log.info(
            "[CORE_FINAL_EXPOSURE] symbol=%s result=PASS positions=%s "
            "active_orders=%s decision_effect=NONE",
            symbol,
            result.metrics.get("active_positions", 0.0),
            result.metrics.get("active_orders", 0.0),
        )
        return True

    @observe_nexus_validation
    async def _nexus_validate(self, sig):
        decision = await super()._nexus_validate(sig)
        await self._prepare_professional_risk(sig, decision)
        return decision
