"""Fail-closed validation for account financial state used before new entries."""
from __future__ import annotations
import math
from dataclasses import dataclass


class FinancialStateInvalid(RuntimeError):
    pass


@dataclass(frozen=True)
class FinancialState:
    equity: float
    available_margin: float
    hwm: float
    drawdown: float


def _finite_nonnegative(value, name: str) -> float:
    if isinstance(value, bool):
        raise FinancialStateInvalid(f"{name}=boolean")
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise FinancialStateInvalid(f"{name}=non_numeric") from exc
    if not math.isfinite(v) or v < 0:
        raise FinancialStateInvalid(f"{name}=invalid:{value!r}")
    return v


def validate_financial_state(*, equity, available_margin, hwm, drawdown) -> FinancialState:
    eq = _finite_nonnegative(equity, "equity")
    margin = _finite_nonnegative(available_margin, "available_margin")
    peak = _finite_nonnegative(hwm, "hwm")
    dd = _finite_nonnegative(drawdown, "drawdown")
    if peak + 1e-12 < eq:
        raise FinancialStateInvalid(f"hwm_below_equity:hwm={peak!r}:equity={eq!r}")
    if dd > 1.0 + 1e-9:
        raise FinancialStateInvalid(f"drawdown_out_of_range:{dd!r}")
    if peak == 0.0:
        if eq != 0.0 or dd != 0.0:
            raise FinancialStateInvalid(
                f"zero_hwm_inconsistent:equity={eq!r}:drawdown={dd!r}"
            )
        expected = 0.0
    else:
        expected = max(0.0, (peak - eq) / peak)
    if not math.isclose(dd, expected, rel_tol=1e-7, abs_tol=1e-9):
        raise FinancialStateInvalid(
            f"drawdown_mismatch:reported={dd!r}:expected={expected!r}:"
            f"equity={eq!r}:hwm={peak!r}"
        )
    return FinancialState(eq, margin, peak, dd)
