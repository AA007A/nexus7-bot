"""Explicit runtime TradingEngine composition for NEXUS-7.

The large canonical engine remains in ``bot.engine``. This runtime subclass
adds post-decision observability declaratively and prepares professional,
stop-aware risk state for the executable core without replacing engine methods
at startup.

No exchange mutation, release state, or execution permission is changed here.
"""
from __future__ import annotations

from bot.account_capital_reader import read_account_capital
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

    @observe_nexus_validation
    async def _nexus_validate(self, sig):
        decision = await super()._nexus_validate(sig)
        await self._prepare_professional_risk(sig, decision)
        return decision
