"""Strict PRODUCTION PROMOTION GATE over a NEXUS OOS replay artifact.

RESEARCH_RESULT vs PRODUCTION_PROMOTION_GATE
--------------------------------------------
``python -m bot.nexus_oos_real_replay_corrected`` is a research command: it
always writes an artifact and exits 0 when the replay itself ran, even when
the evidence is negative. That is correct for research.

This module is the promotion gate. It exits 0 only when every condition below
is met, and non-zero otherwise (including missing or corrupt artifacts):

    python -m bot.nexus_oos_promotion_gate artifacts/nexus_oos_real_replay.json

Uplift of NEXUS over a losing baseline is NOT sufficient: the approved
strategy itself must have positive net expectancy with a positive lower
bootstrap bound (``APPROVED_EXPECTANCY_NOT_POSITIVE`` /
``APPROVED_EXPECTANCY_CI_NOT_POSITIVE``).

Exit codes: 0 PROMOTE, 1 BLOCKED (evidence insufficient/negative),
2 ARTIFACT_MISSING, 3 ARTIFACT_CORRUPT.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

EXIT_PROMOTE = 0
EXIT_BLOCKED = 1
EXIT_MISSING = 2
EXIT_CORRUPT = 3


@dataclass(frozen=True)
class GatePolicy:
    min_baseline_samples: int = 200
    min_approved_samples: int = 100
    min_symbols_contributing: int = 4
    max_symbol_share_of_positive_r: float = 0.50
    max_period_share_of_positive_r: float = 0.50
    min_temporal_folds_positive: int = 3
    min_history_days: float = 60.0
    require_context_parity: bool = True
    cost_stress_scenarios: tuple[str, ...] = ("fees_plus_50pct", "slippage_x2")


@dataclass
class GateResult:
    promote: bool
    blockers: list[str] = field(default_factory=list)
    exit_code: int = EXIT_BLOCKED

    def to_dict(self) -> dict:
        return {
            "gate": "PRODUCTION_PROMOTION_GATE",
            "verdict": "PROMOTE" if self.promote else "BLOCK",
            "blockers": sorted(set(self.blockers)),
            "exit_code": self.exit_code,
        }


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def evaluate(artifact: dict, policy: GatePolicy = GatePolicy()) -> GateResult:
    """Fail-closed evaluation. Any missing evidence field is a blocker."""
    if not isinstance(artifact, dict):
        return GateResult(False, ["ARTIFACT_CORRUPT"], EXIT_CORRUPT)
    b: list[str] = []

    status = artifact.get("status")
    if status != "AI_EDGE_PROVEN":
        b.append("STATUS_NOT_AI_EDGE_PROVEN")
    for blocker in artifact.get("blockers") or []:
        b.append(str(blocker))

    rep = artifact.get("report")
    if not isinstance(rep, dict):
        return GateResult(False, b + ["REPORT_MISSING"], EXIT_CORRUPT)

    base_n = _num(rep.get("known_baseline_outcomes"))
    appr_n = _num(rep.get("known_approved_outcomes"))
    if base_n is None or base_n < policy.min_baseline_samples:
        b.append("INSUFFICIENT_BASELINE_SAMPLE")
    if appr_n is None or appr_n < policy.min_approved_samples:
        b.append("INSUFFICIENT_APPROVED_SAMPLE")

    uplift_lo = _num(rep.get("bootstrap_ci_low_r"))
    if uplift_lo is None or uplift_lo <= 0:
        b.append("UPLIFT_CI_NOT_POSITIVE")

    nexus_exp = _num(rep.get("nexus_expectancy_r"))
    if nexus_exp is None or nexus_exp <= 0:
        b.append("APPROVED_EXPECTANCY_NOT_POSITIVE")

    perf = ((artifact.get("performance") or {}).get("approved")) or {}
    appr_lo = _num(perf.get("expectancy_ci_low_r"))
    if appr_lo is None or appr_lo <= 0:
        b.append("APPROVED_EXPECTANCY_CI_NOT_POSITIVE")

    parity = artifact.get("historical_context_parity_complete")
    if parity is None:
        parity = all(
            bool((s.get("historical_context") or {}).get("parity_complete"))
            for s in artifact.get("symbols") or [{}]
        )
    if policy.require_context_parity and parity is not True:
        b.append("HISTORICAL_CONTEXT_PARITY_INCOMPLETE")

    symbols = artifact.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        b.append("SYMBOLS_MISSING")
    else:
        if any(s.get("error") for s in symbols):
            b.append("SYMBOLS_UNAVAILABLE")
        days = [_num(s.get("history_days")) for s in symbols if not s.get("error")]
        if not days or any(d is None or d < policy.min_history_days for d in days):
            b.append("HISTORY_HORIZON_TOO_SHORT")

    conc = artifact.get("concentration") or {}
    sym_c = conc.get("approved_by_symbol") or {}
    contributing = _num(sym_c.get("groups_positive"))
    if contributing is None or contributing < policy.min_symbols_contributing:
        b.append("TOO_FEW_SYMBOLS_CONTRIBUTING")
    share = _num(sym_c.get("top_share_of_positive_r"))
    if share is None or share > policy.max_symbol_share_of_positive_r:
        b.append("SINGLE_SYMBOL_DOMINATES")
    per_c = conc.get("approved_by_month") or {}
    pshare = _num(per_c.get("top_share_of_positive_r"))
    if pshare is None or pshare > policy.max_period_share_of_positive_r:
        b.append("SINGLE_PERIOD_DOMINATES")

    rob = ((artifact.get("robustness") or {}).get("summary")) or {}
    folds_pos = _num(rob.get("temporal_folds_positive_uplift"))
    if folds_pos is None or folds_pos < policy.min_temporal_folds_positive:
        b.append("TEMPORAL_ROBUSTNESS_INSUFFICIENT")

    stress = artifact.get("cost_stress_approved") or {}
    for name in policy.cost_stress_scenarios:
        exp = _num((stress.get(name) or {}).get("net_expectancy_r"))
        if exp is None or exp <= 0:
            b.append(f"COST_STRESS_FAILS_{name.upper()}")

    method = artifact.get("methodology") or {}
    for flag in ("closed_candles_only", "historical_clock_frozen", "fees_included",
                 "slippage_included"):
        if method.get(flag) is not True:
            b.append(f"METHODOLOGY_{flag.upper()}_NOT_CONFIRMED")

    b = sorted(set(b))
    return GateResult(not b, b, EXIT_PROMOTE if not b else EXIT_BLOCKED)


def evaluate_path(path: str | Path, policy: GatePolicy = GatePolicy()) -> GateResult:
    p = Path(path)
    if not p.is_file():
        return GateResult(False, ["ARTIFACT_MISSING"], EXIT_MISSING)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return GateResult(False, ["ARTIFACT_CORRUPT"], EXIT_CORRUPT)
    result = evaluate(data, policy)
    if not isinstance(data, dict) or "report" not in data:
        result.exit_code = EXIT_CORRUPT
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("artifact")
    parser.add_argument(
        "--allow-incomplete-context-parity", action="store_true",
        help="Research use only. Never passed by the PR promotion workflow.",
    )
    args = parser.parse_args(argv)
    policy = GatePolicy(require_context_parity=not args.allow_incomplete_context_parity)
    result = evaluate_path(args.artifact, policy)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
