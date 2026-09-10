"""RiskManagerV3 for NEXUS-7.

A side-effect-free risk authority that separates equity, available collateral,
position margin and order margin, and sizes strictly from planned stop risk.
No exchange mutation is performed here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from bot.config import cfg
from bot.professional_risk import CapitalState, StopRiskSizingResult, stop_risk_size
from bot.quantity import minimum_base_quantity, quantity_rules


@dataclass(frozen=True)
class RiskSnapshot:
    capital: CapitalState
    peak_equity: float
    drawdown: float
    confirmed: bool


class RiskManagerV3:
    """Canonical risk semantics for new-entry decisions.

    Invariants:
    - drawdown is computed from equity, never available collateral;
    - trade risk budget is equity * risk_pct;
    - required margin is constrained by available collateral;
    - leverage changes collateral usage, not the configured loss budget;
    - sizing returns zero when exchange minimums cannot fit the risk/margin cap.
    """

    def __init__(self) -> None:
        self._capital = CapitalState(0.0, 0.0)
        self._peak_equity = 0.0
        self._confirmed = False

    @property
    def capital(self) -> CapitalState:
        return self._capital

    @property
    def equity(self) -> float:
        return self._capital.equity

    @property
    def available_collateral(self) -> float:
        return self._capital.available_collateral

    @property
    def drawdown(self) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, (self._peak_equity - self.equity) / self._peak_equity)

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    def restore_peak_equity(self, peak_equity: float) -> float:
        """Restore a durable equity high-water mark without confirming capital.

        Restoration can only raise the in-memory peak. It intentionally does
        not mark the current capital snapshot confirmed; a fresh authenticated
        account read is still required before sizing or opening a position.
        """
        if isinstance(peak_equity, bool):
            raise ValueError("peak equity boolean")
        peak = float(peak_equity)
        if not math.isfinite(peak) or peak <= 0:
            raise ValueError("peak equity must be positive and finite")
        self._peak_equity = max(self._peak_equity, peak)
        return self._peak_equity

    def update_capital(self, capital: CapitalState) -> RiskSnapshot:
        capital.validate()
        self._capital = capital
        self._peak_equity = max(self._peak_equity, capital.equity)
        self._confirmed = True
        return self.snapshot()

    def invalidate(self) -> None:
        self._confirmed = False

    def snapshot(self) -> RiskSnapshot:
        return RiskSnapshot(
            capital=self._capital,
            peak_equity=self._peak_equity,
            drawdown=self.drawdown,
            confirmed=self._confirmed,
        )

    def can_open(self, open_positions: int) -> bool:
        if not self._confirmed:
            return False
        if self.equity <= 0 or self.available_collateral <= 0:
            return False
        if self.drawdown >= cfg.MAX_DRAWDOWN:
            return False
        if open_positions >= cfg.MAX_POSITIONS:
            return False
        return True

    def size_for_stop(
        self,
        *,
        symbol: str,
        entry: float,
        stop: float,
        instruments: dict,
        risk_pct: float | None = None,
        leverage: float | None = None,
        max_margin_pct: float | None = None,
        fee_rate_per_side: float = 0.0,
        expected_slippage_pct: float = 0.0,
    ) -> StopRiskSizingResult:
        if not self._confirmed:
            raise RuntimeError("capital state not confirmed")
        info = instruments.get(symbol)
        if not isinstance(info, dict) or not info:
            raise ValueError("instrument metadata unavailable")

        multiplier, lot, _, _ = quantity_rules(info)
        qty_step = float(multiplier * lot)
        min_qty = float(minimum_base_quantity(info, entry))
        if not math.isfinite(qty_step) or qty_step <= 0:
            raise ValueError("invalid quantity step")
        if not math.isfinite(min_qty) or min_qty <= 0:
            raise ValueError("invalid minimum quantity")

        return stop_risk_size(
            capital=self._capital,
            entry=entry,
            stop=stop,
            risk_pct=float(cfg.MAX_RISK_PCT if risk_pct is None else risk_pct),
            leverage=float(cfg.LEVERAGE if leverage is None else leverage),
            qty_step=qty_step,
            min_qty=min_qty,
            max_margin_pct=float(getattr(cfg, "MAX_MARGIN_PCT", 0.80) if max_margin_pct is None else max_margin_pct),
            fee_rate_per_side=fee_rate_per_side,
            expected_slippage_pct=expected_slippage_pct,
        )
