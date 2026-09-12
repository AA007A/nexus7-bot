"""Read-only net-R:R calibration linked to ``opportunity_audit``.

This module runs only as post-decision telemetry. The canonical NEXUS cost
wrapper attaches its exact frozen candidate cost snapshot to the decision before
resetting the internal ContextVar. This module consumes that handoff without any
new exchange/API request. A live ContextVar remains only as a compatibility
fallback for direct/in-scope calls.

It enriches the existing opportunity-audit row and emits calibration evidence;
it never authorizes execution or changes thresholds, leverage, sizing, risk,
orders, positions, or exchange state.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any

from bot import database as db
from bot import nexus_ai
from bot.config import cfg
from bot.missed_opportunity_audit import _signal_key as _opportunity_signal_key
from bot.nexus_live_cost_calibration import _COST_CONTEXT

_OUTCOME_LOGGED: set[str] = set()
_HORIZON_LOGGED: set[tuple[str, str]] = set()
_HORIZONS = (
    ("p15_net_pct", "15m"),
    ("p30_net_pct", "30m"),
    ("p60_net_pct", "60m"),
    ("p120_net_pct", "120m"),
    ("p240_net_pct", "240m"),
)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _rr_net_threshold() -> float:
    """Read the production threshold without mutating configuration."""
    base_rr = _finite(getattr(cfg, "MIN_RR_RATIO", 2.0), 2.0)
    fallback = round(base_rr * 0.80, 2)
    threshold = _finite(os.environ.get("NEXUS_MIN_RR_NET", fallback), fallback)
    return threshold if threshold > 0 else fallback


def _rr_net_bucket(rr_net: float, threshold: float | None = None) -> str:
    """Stable absolute bands around the current 1.60 production gate."""
    rr = _finite(rr_net, -1.0)
    minimum = _finite(threshold, _rr_net_threshold())
    if rr < 0:
        return "UNAVAILABLE"
    if rr >= minimum:
        return "AT_OR_ABOVE_THRESHOLD"
    if rr < 1.20:
        return "LT_1_20"
    if rr < 1.40:
        return "1_20_1_40"
    if rr < 1.50:
        return "1_40_1_50"
    return "1_50_TO_THRESHOLD"


def _rr_gap_bucket(rr_net: float, threshold: float | None = None) -> str:
    minimum = _finite(threshold, _rr_net_threshold())
    rr = _finite(rr_net, -1.0)
    if rr < 0:
        return "UNAVAILABLE"
    gap = minimum - rr
    if gap <= 0:
        return "AT_OR_ABOVE_THRESHOLD"
    if gap <= 0.05:
        return "GAP_0_00_0_05"
    if gap <= 0.10:
        return "GAP_0_05_0_10"
    if gap <= 0.20:
        return "GAP_0_10_0_20"
    if gap <= 0.40:
        return "GAP_0_20_0_40"
    return "GAP_GT_0_40"


def _reason_rr_net(reason: str) -> float | None:
    """Extract the rounded net-R:R printed by the canonical NEXUS gate."""
    match = re.search(
        r"R:R\s+l[ií]quido\s+([0-9]+(?:[\.,][0-9]+)?)",
        str(reason or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    value = _finite(match.group(1).replace(",", "."), -1.0)
    return value if value >= 0 else None


def _cost_context(decision=None):
    """Return the exact decision handoff, falling back to an in-scope ContextVar."""
    handed_off = getattr(decision, "_bgx_nexus_cost_context", None)
    if handed_off is not None:
        return handed_off, "decision_handoff"
    inherited = _COST_CONTEXT.get()
    if inherited is not None:
        return inherited, "contextvar_fallback"
    return None, "unavailable"


def _snapshot(sig, decision=None) -> dict[str, Any]:
    """Compute net R:R from the exact NEXUS cost snapshot with no I/O."""
    threshold = _rr_net_threshold()
    symbol = str(getattr(sig, "symbol", "UNKNOWN"))
    ctx, handoff_source = _cost_context(decision)
    if ctx is None:
        return {
            "available": False,
            "reason": "nexus_cost_context_unavailable",
            "cost_context_handoff": handoff_source,
            "rr_net_threshold": round(threshold, 6),
        }
    if str(getattr(ctx, "symbol", "")) != symbol:
        return {
            "available": False,
            "reason": "nexus_cost_context_symbol_mismatch",
            "cost_context_handoff": handoff_source,
            "rr_net_threshold": round(threshold, 6),
        }

    try:
        result = nexus_ai.expected_value(
            0.50,
            _finite(getattr(sig, "entry", 0.0)),
            _finite(getattr(sig, "sl", 0.0)),
            _finite(getattr(sig, "tp", 0.0)),
            taker_fee=_finite(getattr(ctx, "taker_fee", 0.0)),
            slippage=_finite(getattr(ctx, "slippage", 0.0)),
        )
        rr_net = _finite((result or {}).get("rr_net"), -1.0)
        if rr_net < 0:
            raise ValueError("invalid_rr_net")
        gap = threshold - rr_net
        spread_bps = getattr(ctx, "spread_bps", None)
        return {
            "available": True,
            "rr_net_snapshot": round(rr_net, 6),
            "rr_net_threshold": round(threshold, 6),
            "rr_net_gap_to_threshold": round(gap, 6),
            "rr_net_bucket": _rr_net_bucket(rr_net, threshold),
            "rr_gap_bucket": _rr_gap_bucket(rr_net, threshold),
            "taker_fee_bps": round(_finite(getattr(ctx, "taker_fee", 0.0)) * 10000.0, 4),
            "slippage_bps": round(_finite(getattr(ctx, "slippage", 0.0)) * 10000.0, 4),
            "spread_bps": None if spread_bps is None else round(_finite(spread_bps), 4),
            "fee_source": str(getattr(ctx, "fee_source", "UNKNOWN")),
            "slippage_source": str(getattr(ctx, "slippage_source", "UNKNOWN")),
            "cost_context_handoff": handoff_source,
            "same_nexus_cost_context": True,
            "execution_effect": "NONE",
        }
    except Exception as exc:
        return {
            "available": False,
            "reason": type(exc).__name__,
            "cost_context_handoff": handoff_source,
            "rr_net_threshold": round(threshold, 6),
        }


def _load_metadata(raw: Any) -> dict:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _audit_signal_key(sig, epoch: float | None = None) -> str:
    """Use the exact canonical 15m cohort key owned by opportunity_audit."""
    when = time.time() if epoch is None else float(epoch)
    return _opportunity_signal_key(
        str(getattr(sig, "symbol", "")),
        str(getattr(sig, "direction", "")),
        str(getattr(sig, "entry_type", "UNKNOWN")),
        when,
    )


async def _current_row(sig) -> tuple | None:
    key = _audit_signal_key(sig)
    rows = await db._fetchall(
        """SELECT signal_key,metadata,approved,decision_reason
           FROM opportunity_audit
           WHERE signal_key=? LIMIT 1""",
        (key,),
    )
    return rows[0] if rows else None


async def _emit_available_evidence(log) -> None:
    """Expose new horizon/final outcomes for rows already enriched with R:R data."""
    rows = await db._fetchall(
        """SELECT signal_key,symbol,direction,approved,hypothetical_status,
                  last_net_pct,mfe_pct,mae_pct,p15_net_pct,p30_net_pct,
                  p60_net_pct,p120_net_pct,p240_net_pct,metadata
           FROM opportunity_audit
           WHERE created_epoch>=?
           ORDER BY created_epoch DESC LIMIT 250""",
        (time.time() - 18000.0,),
    )
    for row in rows or []:
        try:
            (key, symbol, direction, approved, status, last_net, mfe, mae,
             p15, p30, p60, p120, p240, metadata_raw) = row
            meta = _load_metadata(metadata_raw)
            cal = meta.get("rr_net_calibration") or {}
            if not isinstance(cal, dict) or cal.get("available") is not True:
                continue
            blocker = str(meta.get("blocker_class") or "UNKNOWN")
            rr_net = _finite(cal.get("rr_net_snapshot"), -1.0)
            threshold = _finite(cal.get("rr_net_threshold"), _rr_net_threshold())
            bucket = str(cal.get("rr_net_bucket") or _rr_net_bucket(rr_net, threshold))
            gap_bucket = str(cal.get("rr_gap_bucket") or _rr_gap_bucket(rr_net, threshold))

            for (field, label), value in zip(_HORIZONS, (p15, p30, p60, p120, p240)):
                marker = (str(key), field)
                if value is None or marker in _HORIZON_LOGGED:
                    continue
                _HORIZON_LOGGED.add(marker)
                log.info(
                    "[RR_GATE_HORIZON] candidate=%s symbol=%s side=%s blocker=%s "
                    "approved=%s horizon=%s net_pct=%.4f rr_net=%.4f rr_min=%.4f "
                    "rr_bucket=%s gap_bucket=%s threshold_unchanged=true "
                    "leverage_unchanged=true execution_effect=NONE",
                    key, symbol, direction, blocker, bool(approved), label,
                    _finite(value), rr_net, threshold, bucket, gap_bucket,
                )

            if str(status or "OPEN") != "OPEN" and str(key) not in _OUTCOME_LOGGED:
                _OUTCOME_LOGGED.add(str(key))
                log.info(
                    "[RR_GATE_OUTCOME] candidate=%s symbol=%s side=%s blocker=%s "
                    "approved=%s status=%s last_net_pct=%.4f mfe=%.4f mae=%.4f "
                    "rr_net=%.4f rr_min=%.4f rr_bucket=%s gap_bucket=%s "
                    "threshold_unchanged=true leverage_unchanged=true execution_effect=NONE",
                    key, symbol, direction, blocker, bool(approved), status,
                    _finite(last_net), _finite(mfe), _finite(mae), rr_net,
                    threshold, bucket, gap_bucket,
                )
        except Exception as exc:
            log.debug(
                "[RR_GATE_CALIBRATION] evidence_row_skipped error=%s execution_effect=NONE",
                type(exc).__name__,
            )


async def observe(engine, sig, decision, log) -> None:
    """Enrich the already-recorded opportunity row; never return trading authority."""
    del engine  # proof-by-interface: no exchange/client access is needed here.
    try:
        row = await _current_row(sig)
        if not row:
            log.debug(
                "[RR_GATE_CALIBRATION] candidate_row_missing symbol=%s candidate=%s "
                "lookup=canonical_15m_signal_key execution_effect=NONE",
                getattr(sig, "symbol", "UNKNOWN"), _audit_signal_key(sig),
            )
            await _emit_available_evidence(log)
            return

        key, metadata_raw, approved, decision_reason = row
        metadata = _load_metadata(metadata_raw)
        snapshot = _snapshot(sig, decision)
        rounded_gate_rr = _reason_rr_net(decision_reason)
        if rounded_gate_rr is not None:
            snapshot["rr_net_gate_reason_rounded"] = rounded_gate_rr
        metadata["rr_net_calibration"] = snapshot

        updated = await db._exec(
            "UPDATE opportunity_audit SET metadata=? WHERE signal_key=?",
            (json.dumps(metadata, separators=(",", ":"), sort_keys=True), key),
        )
        if updated and snapshot.get("available") is True:
            log.info(
                "[RR_GATE_CALIBRATION] candidate=%s symbol=%s side=%s blocker=%s "
                "approved=%s rr_net=%.4f rr_min=%.4f rr_gap=%+.4f rr_bucket=%s "
                "gap_bucket=%s taker_bps=%.3f slippage_bps=%.3f spread_bps=%s "
                "cost_context_handoff=%s same_nexus_cost_context=true "
                "threshold_unchanged=true leverage_unchanged=true execution_effect=NONE",
                key,
                getattr(sig, "symbol", "UNKNOWN"),
                getattr(sig, "direction", "UNKNOWN"),
                metadata.get("blocker_class", "UNKNOWN"),
                bool(approved),
                _finite(snapshot.get("rr_net_snapshot")),
                _finite(snapshot.get("rr_net_threshold")),
                _finite(snapshot.get("rr_net_gap_to_threshold")),
                snapshot.get("rr_net_bucket"),
                snapshot.get("rr_gap_bucket"),
                _finite(snapshot.get("taker_fee_bps")),
                _finite(snapshot.get("slippage_bps")),
                "NA" if snapshot.get("spread_bps") is None else f"{_finite(snapshot.get('spread_bps')):.3f}",
                snapshot.get("cost_context_handoff"),
            )
        elif snapshot.get("available") is not True:
            log.debug(
                "[RR_GATE_CALIBRATION] unavailable symbol=%s reason=%s handoff=%s "
                "threshold_unchanged=true execution_effect=NONE",
                getattr(sig, "symbol", "UNKNOWN"), snapshot.get("reason"),
                snapshot.get("cost_context_handoff"),
            )

        await _emit_available_evidence(log)
    except Exception as exc:
        log.warning(
            "[RR_GATE_CALIBRATION] telemetry_error=%s threshold_unchanged=true "
            "leverage_unchanged=true execution_effect=NONE",
            type(exc).__name__,
        )