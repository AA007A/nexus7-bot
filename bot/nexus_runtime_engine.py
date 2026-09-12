"""Explicit runtime TradingEngine composition for BGX.

The large canonical engine remains in ``bot.engine``. This runtime subclass
adds post-decision observability declaratively and prepares professional,
stop-aware risk state for the executable core without replacing engine methods
at startup.

No exchange mutation, release state, or execution permission is changed here.
"""
from __future__ import annotations

from bot import account_balance_semantics
from bot import missed_opportunity_audit
from bot.account_capital_reader import read_account_capital
from bot.config import cfg
from bot.drawdown_persistence import restore_update_real_account_peak
from bot.engine import TradingEngine as CoreTradingEngine
from bot.kucoin_position_units import KuCoinPositionUnitAdapter
from bot.logger import log
from bot.nexus_validation_observability import observe_nexus_validation
from bot.notifier import notify
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.shadow_balance_semantics import refresh_shadow_risk


class TradingEngine(CoreTradingEngine):
    """Canonical engine plus explicit observability and RiskManagerV3 sizing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # KuCoin currentQty is native contracts while every engine/risk/Position
        # quantity is base asset. Normalize all engine-facing position reads at
        # one explicit runtime boundary. The underlying exchange client and all
        # order-dispatch methods remain untouched/delegated.
        if not isinstance(self.client, KuCoinPositionUnitAdapter):
            self.client = KuCoinPositionUnitAdapter(self.client)
        if not isinstance(self.risk, ProfessionalRiskAdapter):
            self.risk = ProfessionalRiskAdapter(self.risk)

    def _contracts_to_base_qty(self, symbol: str, quantity: float) -> float:
        """Honor the runtime position-unit boundary exactly once.

        The legacy core calls this helper after reading ``get_positions()`` and
        historically expected that read to expose native contracts. In the BGX
        runtime, ``KuCoinPositionUnitAdapter`` already normalizes every
        ``get_positions()[\"size\"]`` to base-asset units and labels the row
        ``sizeUnit=BASE_ASSET``. Reapplying the multiplier here would therefore
        shrink/grow exposure a second time.

        This override is intentionally limited to the composed runtime. The
        unwrapped legacy core keeps its original contracts-to-base conversion.
        """
        if isinstance(self.client, KuCoinPositionUnitAdapter):
            value = float(quantity)
            if value != value or value < 0:
                raise ValueError(
                    f"_contracts_to_base_qty({symbol}): invalid normalized base quantity"
                )
            return value
        return super()._contracts_to_base_qty(symbol, quantity)

    async def _update_balance(self):
        """Refresh real-account risk from equity and durable high-water mark.

        The legacy core used ``get_balance()`` (free/available collateral) for
        drawdown. That is not the same accounting concept as account equity and
        also reset its high-water mark on restart. PAPER keeps the legacy path.
        SHADOW remains mutation-free and uses the same authenticated read-only
        equity semantics through ``refresh_shadow_risk``.

        In controlled LIVE, account drawdown is deliberately advisory-only:
        the durable high-water mark, drawdown percentage, one-shot alert and all
        telemetry remain active, but MAX_DRAWDOWN does not mutate ``active`` or
        veto entries by itself. Independent daily-stop, integrity, NEXUS,
        protection, ownership and market-risk gates remain authoritative.
        """
        if getattr(self, "paper_trade", False):
            return await super()._update_balance()

        try:
            if getattr(self, "_validation_safety_lock_active", False):
                await refresh_shadow_risk(self)
                return

            state = await account_balance_semantics.read_account_state(self.client)
            equity = float(state["equity"])
            self.risk.update(equity)
            await restore_update_real_account_peak(self.risk, equity, strict=True)

            if equity > 0:
                self.daily_target = round(equity * cfg.DAILY_TARGET_PCT, 2)
                self.daily_stop_loss = round(equity * cfg.DAILY_STOP_LOSS_PCT, 2)

            if self.risk.drawdown >= cfg.MAX_DRAWDOWN:
                if not getattr(self, "_dd_alerted", False):
                    self._dd_alerted = True
                    log.warning(
                        "[DRAWDOWN_ADVISORY_RUNTIME] drawdown=%.2f%% configured_limit=%.2f%% "
                        "entries_blocked=false active_unchanged=true execution_effect=NONE",
                        self.risk.drawdown * 100.0,
                        cfg.MAX_DRAWDOWN * 100.0,
                    )
                    await notify(
                        f"⚠️ *DRAWDOWN ELEVADO — ADVISORY*\n"
                        f"`{'━'*28}`\n"
                        f"📉 Drawdown:     `{self.risk.drawdown:.1%}`\n"
                        f"💼 Equity:       `${equity:,.2f} USDT`\n"
                        f"`{'━'*28}`\n"
                        f"_BGX continua operando; drawdown não bloqueia novas entradas._"
                    )
            else:
                self._dd_alerted = False
        except Exception as exc:
            self.risk.balance_confirmed = False
            self.risk.invalidate_capital()
            log.error("[DURABLE_DRAWDOWN] balance refresh blocked: %s", type(exc).__name__)
            raise

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
            await restore_update_real_account_peak(
                self.risk, snapshot.capital.equity, strict=True
            )
            if not self.risk._v3.can_open(len(self.positions)):
                self.risk.invalidate_capital()
                raise RuntimeError("durable drawdown/capital gate blocked V3 sizing")
        except Exception:
            self.risk.invalidate_capital()
            raise

    @observe_nexus_validation
    async def _nexus_validate(self, sig):
        decision = await super()._nexus_validate(sig)
        # Counterfactual opportunity tracking is deliberately best-effort and
        # isolated from the trading decision. It records both approvals and
        # rejections so future gate calibration has a control group.
        await missed_opportunity_audit.observe(self, sig, decision, log)
        await self._prepare_professional_risk(sig, decision)
        return decision
