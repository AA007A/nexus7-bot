"""Fail-closed capacity checks for the real-money pilot.

This module does not grant execution permission. It only adds additional
blocking reasons to PilotGuard.evaluate() based on total account exposure,
including external/manual positions, and on fresh KuCoin account-overview
capital ratios.
"""
import os
import time

from bot.pilot import PILOT_MAX_CONCURRENT_POSITIONS
from bot.professional_risk import capital_state_from_account_overview


MIN_AVAILABLE_EQUITY_RATIO = float(
    os.environ.get("PILOT_MIN_AVAILABLE_EQUITY_RATIO", "0.20")
)
MAX_POSITION_MARGIN_EQUITY_RATIO = float(
    os.environ.get("PILOT_MAX_POSITION_MARGIN_EQUITY_RATIO", "0.80")
)
MAX_ACCOUNT_SNAPSHOT_AGE_S = float(
    os.environ.get("PILOT_MAX_ACCOUNT_SNAPSHOT_AGE_S", "60")
)


def _external_symbols(engine):
    integrity = getattr(engine, "integrity", None)
    state = getattr(integrity, "state", None)
    issues = getattr(state, "issues", None) or []
    out = set()
    for issue in issues:
        code = str(getattr(issue, "code", "") or "")
        if not code.startswith("EXTERNAL_POSITION_"):
            continue
        detail = str(getattr(issue, "detail", "") or "")
        symbol = detail.split(":", 1)[0].strip()
        if symbol:
            out.add(symbol)
    return out


def _capital_reasons(client):
    reasons = []
    snap = getattr(client, "_last_account_overview_snapshot", None)
    if not isinstance(snap, dict):
        return ["PILOT_CAPITAL_SNAPSHOT: account overview ausente (fail-closed)"]

    observed_at = snap.get("_observed_at")
    try:
        age = time.time() - float(observed_at)
    except (TypeError, ValueError):
        return ["PILOT_CAPITAL_SNAPSHOT: timestamp inválido (fail-closed)"]
    if age < 0 or age > MAX_ACCOUNT_SNAPSHOT_AGE_S:
        reasons.append(
            f"PILOT_CAPITAL_SNAPSHOT: snapshot com {age:.0f}s; "
            f"máx {MAX_ACCOUNT_SNAPSHOT_AGE_S:.0f}s"
        )

    # Reuse the same KuCoin normalization used by RiskManagerV3. Raw
    # positionMargin can be transiently negative in cross-margin snapshots;
    # treating that raw value as a negative exposure would incorrectly bypass
    # the pilot margin-capacity gate. Any invalid/non-finite capital state is
    # fail-closed here.
    try:
        capital = capital_state_from_account_overview(snap)
    except (TypeError, ValueError, ArithmeticError) as exc:
        reasons.append(
            f"PILOT_CAPITAL_VALUES: account overview inválido "
            f"({type(exc).__name__}) (fail-closed)"
        )
        return reasons

    equity = capital.equity
    available = capital.available_collateral
    committed_margin = capital.committed_margin

    if equity <= 0:
        reasons.append(f"PILOT_CAPITAL_EQUITY: equity={equity:.4f}")
        return reasons

    available_ratio = available / equity
    if available_ratio < MIN_AVAILABLE_EQUITY_RATIO:
        reasons.append(
            f"PILOT_AVAILABLE_CAPACITY: available/equity={available_ratio:.2%} "
            f"< mínimo {MIN_AVAILABLE_EQUITY_RATIO:.2%}"
        )

    margin_ratio = committed_margin / equity
    if margin_ratio > MAX_POSITION_MARGIN_EQUITY_RATIO:
        reasons.append(
            f"PILOT_MARGIN_CAPACITY: committedMargin/equity={margin_ratio:.2%} "
            f"> máximo {MAX_POSITION_MARGIN_EQUITY_RATIO:.2%}"
        )
    return reasons


def install(PilotGuard, log):
    if getattr(PilotGuard, "_exposure_capacity_patched", False):
        return

    original_evaluate = PilotGuard.evaluate

    def _evaluate_with_capacity(self, engine, client, symbol: str, ai_decision=None):
        reasons = list(original_evaluate(self, engine, client, symbol, ai_decision))
        if not self.enabled:
            return reasons

        local_symbols = set((getattr(engine, "positions", {}) or {}).keys())
        external_symbols = _external_symbols(engine)
        total_open = len(local_symbols | external_symbols)

        if total_open >= PILOT_MAX_CONCURRENT_POSITIONS:
            reason = (
                f"PILOT_TOTAL_CONCURRENT: total_open={total_open} "
                f"(local={len(local_symbols)}, external={len(external_symbols)}), "
                f"máx {PILOT_MAX_CONCURRENT_POSITIONS}; nova entrada excederia o piloto"
            )
            if reason not in reasons:
                reasons.append(reason)

        for reason in _capital_reasons(client):
            if reason not in reasons:
                reasons.append(reason)

        self.state.blocked_reasons = reasons
        return reasons

    PilotGuard.evaluate = _evaluate_with_capacity
    PilotGuard._exposure_capacity_patched = True
    log.warning(
        "[PILOT_EXPOSURE_CAPACITY] installed: external positions count toward "
        "pilot concurrency; fresh normalized available/equity and "
        "committedMargin/equity gates active"
    )
