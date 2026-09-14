"""Robustness diagnostics for NEXUS OOS incremental-edge evidence.

Research-only.  These helpers decompose an already leakage-safe candidate replay
without changing live thresholds, execution permissions, leverage, sizing or
exchange state.  The objective is to expose concentration: a pooled uplift can
look attractive while being driven by one symbol or one short market window.
"""
from __future__ import annotations

from dataclasses import asdict
from math import ceil
from typing import Iterable

from bot.nexus_oos_edge_gate import CandidateOutcome, build_edge_report


def _report(rows: Iterable[CandidateOutcome], *, seed: int) -> dict:
    rep = build_edge_report(rows, bootstrap_samples=2500, seed=seed)
    out = asdict(rep)
    out["positive_uplift"] = (
        rep.expectancy_uplift_r is not None and rep.expectancy_uplift_r > 0.0
    )
    out["ci_strictly_positive"] = (
        rep.bootstrap_ci_low_r is not None and rep.bootstrap_ci_low_r > 0.0
    )
    return out


def _chronological_folds(tagged_rows: list[tuple[str, CandidateOutcome]], folds: int) -> list[dict]:
    if folds < 2 or not tagged_rows:
        return []
    timestamps = sorted({float(row.timestamp) for _, row in tagged_rows})
    if len(timestamps) < folds:
        return []

    result: list[dict] = []
    for fold in range(folds):
        start_i = (len(timestamps) * fold) // folds
        end_i = (len(timestamps) * (fold + 1)) // folds
        selected_ts = set(timestamps[start_i:end_i])
        rows = [row for _, row in tagged_rows if float(row.timestamp) in selected_ts]
        if not rows:
            continue
        rep = _report(rows, seed=700 + fold)
        result.append({
            "fold": fold + 1,
            "start_timestamp": min(selected_ts),
            "end_timestamp": max(selected_ts),
            "report": rep,
        })
    return result


def analyze_robustness(symbol_reports: list[dict], *, temporal_folds: int = 4) -> dict:
    """Return symbol, time-fold and leave-one-symbol-out edge decomposition."""
    tagged: list[tuple[str, CandidateOutcome]] = []
    per_symbol: dict[str, dict] = {}

    for idx, rep in enumerate(symbol_reports):
        symbol = str(rep.get("symbol", f"symbol_{idx}"))
        rows = list(rep.get("candidates", []) or [])
        if not rows:
            continue
        tagged.extend((symbol, row) for row in rows)
        per_symbol[symbol] = _report(rows, seed=100 + idx)

    all_rows = [row for _, row in tagged]
    pooled = _report(all_rows, seed=7) if all_rows else None
    folds = _chronological_folds(tagged, temporal_folds)

    leave_one_out: dict[str, dict] = {}
    symbols = sorted(per_symbol)
    for idx, omitted in enumerate(symbols):
        rows = [row for symbol, row in tagged if symbol != omitted]
        leave_one_out[omitted] = _report(rows, seed=500 + idx) if rows else {}

    symbol_positive = sum(1 for rep in per_symbol.values() if rep.get("positive_uplift"))
    symbol_ci_positive = sum(1 for rep in per_symbol.values() if rep.get("ci_strictly_positive"))
    fold_positive = sum(1 for fold in folds if fold["report"].get("positive_uplift"))
    fold_ci_positive = sum(1 for fold in folds if fold["report"].get("ci_strictly_positive"))
    loo_positive = sum(1 for rep in leave_one_out.values() if rep.get("positive_uplift"))
    loo_ci_positive = sum(1 for rep in leave_one_out.values() if rep.get("ci_strictly_positive"))

    # Descriptive stability flag, deliberately not a live/promotion authority.
    # It asks for positive point-estimate uplift in every leave-one-symbol-out
    # sample and at least 75% of chronological folds. CI stability is reported
    # separately because subgroup sample sizes can be much smaller.
    required_positive_folds = ceil(0.75 * len(folds)) if folds else 0
    stable_point_estimate = bool(
        pooled
        and pooled.get("positive_uplift")
        and leave_one_out
        and loo_positive == len(leave_one_out)
        and folds
        and fold_positive >= required_positive_folds
    )

    return {
        "pooled": pooled,
        "per_symbol": per_symbol,
        "chronological_folds": folds,
        "leave_one_symbol_out": leave_one_out,
        "summary": {
            "symbols_evaluated": len(per_symbol),
            "symbols_positive_uplift": symbol_positive,
            "symbols_ci_strictly_positive": symbol_ci_positive,
            "temporal_folds_evaluated": len(folds),
            "temporal_folds_positive_uplift": fold_positive,
            "temporal_folds_ci_strictly_positive": fold_ci_positive,
            "leave_one_symbol_out_evaluated": len(leave_one_out),
            "leave_one_symbol_out_positive_uplift": loo_positive,
            "leave_one_symbol_out_ci_strictly_positive": loo_ci_positive,
            "stable_positive_point_estimate": stable_point_estimate,
            "promotion_authority": False,
            "execution_effect": "NONE",
        },
    }
