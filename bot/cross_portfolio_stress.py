"""Fail-closed portfolio stress gate for a second LIVE CROSS position.

KuCoin liquidates CROSS futures from the account-level risk rate, not from an
isolated per-position liquidation price.  This module therefore authorizes a
second pilot position only after final sizing is known and only when a
conservative stop-stress projection remains below the configured safety limit.

The projection assumes the candidate and every currently open bot position move
to their protective stops at the same time.  It uses private KuCoin account and
position data, exact CROSS margin requirements for the stressed notionals, and
includes expected opening/closing taker fees. Any missing, stale, inconsistent,
non-CROSS, unprotected, or additionally exposed state fails closed.

This module never changes leverage, quantity, SL/TP, strategy/NEXUS thresholds,
or order execution. It only returns PASS/BLOCK at the existing final
pre-dispatch refresh boundary.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from bot.config import cfg
from bot.kucoin import TAKER_FEE


# Retained name for imports; the authoritative value comes from the canonical
# risk policy (env MAX_STOP_STRESS_RISK_RATE, default 0.50, ceiling 0.90).
MAX_STOP_STRESS_RISK_RATE = 0.50


def max_stop_stress_risk_rate() -> float:
    """Configured stop-stress risk-rate ceiling; invalid policy raises.

    KuCoin CROSS semantics: account risk rate = maintenance margin / margin
    balance (equity incl. unrealized PnL); forced liquidation begins at 100%.
    The stress projection below is therefore compared against a fraction of
    that liquidation point. 0.50 means: after every bot position (plus the
    candidate) stops out simultaneously, with fees and slippage, maintenance
    must still be covered twice over.
    """
    from bot.risk_policy import load_policy

    policy = load_policy(cfg)
    violations = [v for v in policy.violations() if "MAX_STOP_STRESS_RISK_RATE" in v]
    if violations:
        raise ValueError(violations[0])
    return float(policy.max_stop_stress_risk_rate)


def _slippage_rate() -> float:
    from bot.risk_policy import load_policy

    return float(load_policy(cfg).expected_slippage_pct)


@dataclass(frozen=True)
class StressResult:
    allowed: bool
    reason: str
    risk_rate: float = 1.0
    stressed_margin: float = 0.0
    maintenance: float = 0.0
    closing_fees: float = 0.0
    opening_fee: float = 0.0
    existing_positions: int = 0
    slippage: float = 0.0
    limit: float = float("nan")


def _positive(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "dataList"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _active_orders(payload):
    return [
        row for row in _rows(payload)
        if isinstance(row, dict)
        and bool(row.get("isActive", str(row.get("status", "")).lower() in {"open", "active"}))
        and not bool(row.get("reduceOnly", False))
    ]


def _select_requirement(payload, kucoin_symbol: str):
    for row in _rows(payload):
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")) != str(kucoin_symbol):
            continue
        mmr = _positive(row.get("mmr"))
        position_value = _positive(row.get("positionValue"))
        if mmr is None or not (0 < mmr < 1):
            return None
        return {"mmr": mmr, "position_value": position_value}
    return None


def _kucoin_symbol(engine, standard_symbol: str) -> str:
    info = (getattr(engine, "instruments", {}) or {}).get(standard_symbol, {})
    if not isinstance(info, dict):
        return ""
    return str(info.get("kucoinSymbol", "") or "")


def _standard_symbol(engine, kucoin_symbol: str) -> str:
    for symbol, info in (getattr(engine, "instruments", {}) or {}).items():
        if isinstance(info, dict) and str(info.get("kucoinSymbol", "")) == str(kucoin_symbol):
            return str(symbol)
    return ""


def _effective_stop(position):
    trailing = _positive(getattr(position, "trailing_sl", None))
    original = _positive(getattr(position, "sl", None))
    if trailing is not None:
        return trailing
    return original


def _stop_is_adverse(direction: str, mark: float, stop: float) -> bool:
    side = str(direction).upper()
    if side == "LONG":
        return 0 < stop < mark
    if side == "SHORT":
        return stop > mark > 0
    return False


def _candidate_geometry(sig, qty: float):
    entry = _positive(getattr(sig, "entry", None))
    stop = _positive(getattr(sig, "sl", None))
    qty = _positive(qty)
    direction = str(getattr(sig, "direction", "")).upper()
    if entry is None or stop is None or qty is None or direction not in {"LONG", "SHORT"}:
        return None
    if direction == "LONG" and not stop < entry:
        return None
    if direction == "SHORT" and not stop > entry:
        return None
    value_entry = qty * entry
    value_stop = qty * stop
    pnl_to_stop = -qty * abs(entry - stop)
    return {
        "entry": entry,
        "stop": stop,
        "qty": qty,
        "direction": direction,
        "value_entry": value_entry,
        "value_stop": value_stop,
        "pnl_to_stop": pnl_to_stop,
    }


async def _margin_requirement(client, kucoin_symbol: str, position_value: float):
    response = await client._post(
        "/api/v2/getCrossModeMarginRequirement",
        {
            "symbol": kucoin_symbol,
            "positionValue": f"{float(position_value):.8f}",
            "leverage": str(int(cfg.LEVERAGE)),
        },
    )
    selected = _select_requirement(response, kucoin_symbol)
    if selected is None:
        raise ValueError("cross_margin_requirement_unavailable")
    return selected


async def evaluate(engine, sig, qty: float) -> StressResult:
    """Project account CROSS risk with all bot positions simultaneously at SL."""
    client = getattr(engine, "client", None)
    if client is None:
        return StressResult(False, "missing_exchange_client")

    candidate = _candidate_geometry(sig, qty)
    if candidate is None:
        return StressResult(False, "invalid_candidate_geometry")

    positions_local = getattr(engine, "positions", {}) or {}
    if not isinstance(positions_local, dict) or len(positions_local) != 1:
        return StressResult(False, "expected_exactly_one_existing_position")
    if str(getattr(sig, "symbol", "")) in positions_local:
        return StressResult(False, "same_symbol_position_conflict")

    try:
        account = await client._get(
            "/api/v1/account-overview", {"currency": "USDT"}, auth=True
        )
        total_margin = None
        if isinstance(account, dict):
            for key in ("marginBalance", "accountEquity", "equity"):
                total_margin = _positive(account.get(key))
                if total_margin is not None:
                    break
        if total_margin is None:
            return StressResult(False, "invalid_total_margin")

        active_payload = await client._get(
            "/api/v1/orders", {"status": "active", "pageSize": "100"}, auth=True
        )
        active = _active_orders(active_payload)
        if active:
            return StressResult(False, "additional_active_order_exposure")

        raw_positions = await client._get(
            "/api/v1/positions", {"currency": "USDT"}, auth=True
        )
    except Exception as exc:
        return StressResult(False, f"private_state_{type(exc).__name__}")

    open_rows = []
    for row in _rows(raw_positions):
        if not isinstance(row, dict):
            continue
        qty_raw = _finite(row.get("currentQty"))
        if qty_raw is not None and abs(qty_raw) > 0:
            open_rows.append(row)

    if len(open_rows) != len(positions_local):
        return StressResult(False, "exchange_local_position_count_mismatch")

    try:
        limit = max_stop_stress_risk_rate()
        slippage_rate = _slippage_rate()
    except (ValueError, TypeError) as exc:
        return StressResult(False, f"risk_policy_invalid_{type(exc).__name__}")

    stressed_margin = float(total_margin)
    maintenance = 0.0
    closing_fees = 0.0
    slippage = 0.0

    for row in open_rows:
        kc_symbol = str(row.get("symbol", "") or "")
        symbol = _standard_symbol(engine, kc_symbol)
        local = positions_local.get(symbol) if symbol else None
        if local is None:
            return StressResult(False, "unmapped_existing_position")

        cross_mode = row.get("crossMode")
        margin_mode = str(row.get("marginMode", "") or "").upper()
        if cross_mode is not True and margin_mode != "CROSS":
            return StressResult(False, "existing_position_not_confirmed_cross")

        raw_contracts = _finite(row.get("currentQty"))
        mark = _positive(row.get("markPrice"))
        mark_value = _positive(row.get("markValue"))
        if raw_contracts is None or raw_contracts == 0 or mark is None or mark_value is None:
            return StressResult(False, "invalid_existing_position_state")

        direction = "LONG" if raw_contracts > 0 else "SHORT"
        if str(getattr(local, "direction", "")).upper() != direction:
            return StressResult(False, "existing_position_side_mismatch")
        stop = _effective_stop(local)
        if stop is None or not _stop_is_adverse(direction, mark, stop):
            return StressResult(False, "existing_stop_not_adverse_or_missing")

        info = (getattr(engine, "instruments", {}) or {}).get(symbol, {})
        multiplier = _positive(info.get("multiplier")) if isinstance(info, dict) else None
        if multiplier is None:
            return StressResult(False, "missing_contract_multiplier")
        base_qty = abs(raw_contracts) * multiplier

        if direction == "LONG":
            pnl_delta = base_qty * (stop - mark)
        else:
            pnl_delta = base_qty * (mark - stop)
        stressed_margin += pnl_delta

        stressed_value = abs(mark_value) * stop / mark
        try:
            requirement = await _margin_requirement(client, kc_symbol, stressed_value)
        except Exception as exc:
            return StressResult(False, f"existing_requirement_{type(exc).__name__}")

        current_mmr = _positive(row.get("maintMarginReq")) or _positive(row.get("maintainMargin"))
        stressed_mmr = requirement["mmr"]
        if current_mmr is not None and 0 < current_mmr < 1:
            stressed_mmr = max(stressed_mmr, current_mmr)
        maintenance += stressed_value * stressed_mmr
        closing_fees += stressed_value * float(TAKER_FEE)
        slippage += stressed_value * slippage_rate

    candidate_kc = _kucoin_symbol(engine, str(getattr(sig, "symbol", "")))
    if not candidate_kc:
        return StressResult(False, "missing_candidate_kucoin_symbol")

    requirement_value = max(candidate["value_entry"], candidate["value_stop"])
    try:
        candidate_requirement = await _margin_requirement(
            client, candidate_kc, requirement_value
        )
    except Exception as exc:
        return StressResult(False, f"candidate_requirement_{type(exc).__name__}")

    stressed_margin += candidate["pnl_to_stop"]
    candidate_mmr = candidate_requirement["mmr"]
    maintenance += candidate["value_stop"] * candidate_mmr
    closing_fees += candidate["value_stop"] * float(TAKER_FEE)
    slippage += candidate["value_stop"] * slippage_rate
    opening_fee = candidate["value_entry"] * float(TAKER_FEE)

    # Stop fills are market fills: slippage is realized loss on top of the
    # stop distance and reduces the stressed margin balance.
    denominator = stressed_margin - opening_fee - slippage
    if not math.isfinite(denominator) or denominator <= 0:
        return StressResult(
            False, "nonpositive_stressed_margin", 1.0, stressed_margin,
            maintenance, closing_fees, opening_fee, len(open_rows), slippage, limit,
        )

    risk_rate = (maintenance + closing_fees) / denominator
    if not math.isfinite(risk_rate) or risk_rate < 0:
        return StressResult(False, "invalid_projected_risk_rate")

    allowed = risk_rate < limit
    return StressResult(
        allowed=allowed,
        reason="stop_stress_headroom_ok" if allowed else "stop_stress_risk_rate_too_high",
        risk_rate=risk_rate,
        stressed_margin=stressed_margin,
        maintenance=maintenance,
        closing_fees=closing_fees,
        opening_fee=opening_fee,
        existing_positions=len(open_rows),
        slippage=slippage,
        limit=limit,
    )


def install(TradingEngine, log) -> None:
    """Install after pilot final-quantity hardening and CROSS risk hardening."""
    if getattr(TradingEngine, "_cross_portfolio_stress_installed", False):
        return

    from bot import pilot_risk_cap_hardening as risk_cap

    original_refresh = TradingEngine._refresh_entry_balance

    async def _refresh_with_cross_portfolio_stress(self, *args, **kwargs):
        sig = risk_cap._PILOT_SIGNAL.get()
        qty = risk_cap._PILOT_FINAL_QTY.get()
        requires_stress = bool(
            sig is not None
            and getattr(sig, "_cross_multi_position_requires_stress", False)
        )

        # PRE_SIZING: qty is None. Preserve the normal balance refresh path.
        if not requires_stress or qty is None:
            return await original_refresh(self, *args, **kwargs)

        try:
            result = await evaluate(self, sig, float(qty))
        except Exception as exc:
            log.critical(
                "[CROSS_PORTFOLIO_STRESS] symbol=%s result=BLOCK reason=unexpected_%s "
                "stage=FINAL_PREDISPATCH execution_effect=BLOCK_NEW_LIVE_ENTRY",
                getattr(sig, "symbol", "?"), type(exc).__name__,
            )
            return False

        if not result.allowed:
            log.warning(
                "[CROSS_PORTFOLIO_STRESS] symbol=%s result=BLOCK reason=%s "
                "risk_rate=%.2f%% limit=%.2f%% stressed_margin=%.4f "
                "maintenance=%.4f closing_fees=%.4f opening_fee=%.4f "
                "existing_positions=%d stage=FINAL_PREDISPATCH "
                "execution_effect=BLOCK_NEW_LIVE_ENTRY",
                getattr(sig, "symbol", "?"), result.reason,
                result.risk_rate * 100.0, result.limit * 100.0,
                result.stressed_margin, result.maintenance, result.closing_fees,
                result.opening_fee, result.existing_positions,
            )
            return False

        log.warning(
            "[CROSS_PORTFOLIO_STRESS] symbol=%s result=PASS reason=%s "
            "risk_rate=%.2f%% limit=%.2f%% stressed_margin=%.4f "
            "maintenance=%.4f closing_fees=%.4f opening_fee=%.4f "
            "existing_positions=%d simultaneous_stop_stress=true "
            "leverage_unchanged=%sx sizing_unchanged=true execution_effect=NONE",
            getattr(sig, "symbol", "?"), result.reason,
            result.risk_rate * 100.0, result.limit * 100.0,
            result.stressed_margin, result.maintenance, result.closing_fees,
            result.opening_fee, result.existing_positions, int(cfg.LEVERAGE),
        )

        # Existing final balance + spread/depth/drift guards remain authoritative.
        return await original_refresh(self, *args, **kwargs)

    TradingEngine._refresh_entry_balance = _refresh_with_cross_portfolio_stress
    TradingEngine._cross_portfolio_stress_capable = True
    TradingEngine._cross_portfolio_stress_installed = True
    log.warning(
        "[CROSS_PORTFOLIO_STRESS] installed=true max_stop_stress_risk_rate=policy(default_50pct,ceiling_90pct) "
        "slippage_included=true "
        "scenario=all_existing_and_candidate_positions_at_protective_stops "
        "active_nonreduce_orders=BLOCK exact_cross_requirements=true "
        "missing_or_inconsistent_state=BLOCK leverage_unchanged=true "
        "sizing_unchanged=true thresholds_unchanged=true"
    )
