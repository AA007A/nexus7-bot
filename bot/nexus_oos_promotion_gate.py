"""Strict PRODUCTION PROMOTION GATE over a NEXUS OOS replay artifact.

RESEARCH_RESULT vs PRODUCTION_PROMOTION_GATE
--------------------------------------------
``python -m bot.nexus_oos_real_replay_corrected`` is a research command: it
always writes an artifact and exits 0 when the replay itself ran, even when
the evidence is negative. That is correct for research.

This module is the promotion gate. It exits 0 only when every condition below
is met, and non-zero otherwise (including missing or corrupt artifacts):

    python -m bot.nexus_oos_promotion_gate artifacts/nexus_oos_real_replay.json

Uplift of NEXUS over a losing baseline is NOT sufficient. Promotion needs
BOTH layers:

* CANDIDATE_RESEARCH (signal edge, RESOLVED outcomes only): approved
  expectancy > 0; horizon-aware block authority lower bound > 0, recomputed
  here from block intervals whose length >= the max resolved outcome horizon
  (re-derived by the gate) and that span >= 30 independent blocks; uplift
  authority lower bound > 0; censoring not material; temporal folds.
* PORTFOLIO_EXECUTION_REPLAY (executable edge): net expectancy > 0, marked
  AND realized final equity > start, no material portfolio censoring,
  walk-forward folds positive (the path bootstrap is non-authoritative), max
  equity drawdown within the research limit, enough trades and contributing
  symbols, accounting invariants proven.
* POLICY PARITY: a sha256-verified LIVE non-secret policy attestation that
  matches the replay manifest.
* REPLAY PARITY: exit parity, pre-trade gate parity and portfolio parity
  complete; pinned replay policy manifest present; closed-candle sentinel
  verified.

IID row-bootstrap intervals are diagnostics with ZERO authority: the gate
never reads them, and legacy IID blocker codes are ignored, so IID can turn
neither a BLOCK into a PROMOTE nor a PROMOTE into a BLOCK.

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
    min_approved_unique_blocks: int = 60
    min_approved_effective_n: float = 100.0
    min_portfolio_trades: int = 50
    min_portfolio_contributing_symbols: int = 4
    min_symbols_contributing: int = 4
    max_symbol_share_of_positive_r: float = 0.50
    max_period_share_of_positive_r: float = 0.50
    min_temporal_folds_positive: int = 3
    min_independent_blocks: int = 30
    min_walk_forward_folds_positive: int = 3
    require_live_policy_attestation: bool = True
    min_history_days: float = 150.0
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


# Legacy IID-derived codes. If a (malformed) artifact still carries them they
# are ignored: IID has zero promotion authority in either direction.
LEGACY_IID_BLOCKERS = frozenset({"UPLIFT_NOT_STATISTICALLY_POSITIVE", "UPLIFT_CI_NOT_POSITIVE"})
REQUIRED_AUTHORITY_MODEL = "HORIZON_AWARE_BLOCK_BOOTSTRAP_V2"
DAY_MS = 86_400_000


def required_block_ms(max_resolved_horizon_ms) -> int | None:
    mx = _num(max_resolved_horizon_ms)
    if mx is None:
        return None
    return max(1, math.ceil(mx / DAY_MS)) * DAY_MS


def block_only_authority(section: dict, *, required_ms: int | None,
                         min_blocks: int) -> tuple[float | None, float | None, str]:
    """Recompute the authority interval from BLOCK intervals only.

    An interval counts only if its block length >= ``required_ms`` (the
    dependence horizon re-derived by the gate) AND it spans >= ``min_blocks``
    independent blocks AND its CI is finite. Never reads ``iid_ci``.
    Returns (low, high, status); (None, None, reason) fails closed.
    """
    if required_ms is None:
        return None, None, "OUTCOME_HORIZON_UNKNOWN"
    ivs = (section or {}).get("block_intervals")
    if not isinstance(ivs, list) or not ivs:
        return None, None, "BLOCK_INTERVALS_MISSING"
    valid, long_enough = [], []
    for iv in ivs:
        if not isinstance(iv, dict):
            continue
        ms = _num(iv.get("block_ms"))
        ci = iv.get("ci") or [None, None]
        n_blk = _num(iv.get("independent_blocks"))
        if ms is None or ms < required_ms:
            continue
        long_enough.append(iv)
        lo, hi = (_num(ci[0]), _num(ci[1])) if len(ci) == 2 else (None, None)
        if n_blk is None or n_blk < min_blocks or lo is None or hi is None:
            continue
        valid.append((lo, hi))
    if valid:
        return min(lo for lo, _ in valid), max(hi for _, hi in valid), "VALID"
    if not long_enough:
        return None, None, "AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON"
    return None, None, "INSUFFICIENT_INDEPENDENT_BLOCKS"


def _authority(section: dict, name: str, b: list, *, required_ms, min_blocks) -> float | None:
    lo, _, status = block_only_authority(section, required_ms=required_ms, min_blocks=min_blocks)
    if status != "VALID":
        b.append(status)
    reported = _num((section or {}).get("authority_ci_low"))
    if lo is not None and (reported is None or abs(lo - reported) > 1e-9):
        b.append(f"{name}_AUTHORITY_CI_INCONSISTENT")
    if lo is None and reported is not None:
        b.append(f"{name}_AUTHORITY_CI_INCONSISTENT")
    return lo


def evaluate(artifact: dict, policy: GatePolicy = GatePolicy()) -> GateResult:
    """Fail-closed evaluation. Any missing evidence field is a blocker.

    Statistical authority: UTC-block bootstrap intervals only (recomputed
    here from the block intervals; IID is never read).
    """
    if not isinstance(artifact, dict):
        return GateResult(False, ["ARTIFACT_CORRUPT"], EXIT_CORRUPT)
    b: list[str] = []

    if artifact.get("authority_model") != REQUIRED_AUTHORITY_MODEL:
        b.append("AUTHORITY_MODEL_UNSUPPORTED")
    if artifact.get("status") != "AI_EDGE_PROVEN":
        b.append("STATUS_NOT_AI_EDGE_PROVEN")
    for blocker in artifact.get("blockers") or []:
        if str(blocker) not in LEGACY_IID_BLOCKERS:
            b.append(str(blocker))

    cand = artifact.get("candidate_research")
    if not isinstance(cand, dict):
        return GateResult(False, sorted(set(b + ["CANDIDATE_RESEARCH_MISSING"])), EXIT_CORRUPT)

    perf = cand.get("performance") or {}
    base_n = _num((perf.get("baseline") or {}).get("trades"))
    appr_n = _num((perf.get("approved") or {}).get("trades"))
    if base_n is None or base_n < policy.min_baseline_samples:
        b.append("INSUFFICIENT_BASELINE_SAMPLE")
    if appr_n is None or appr_n < policy.min_approved_samples:
        b.append("INSUFFICIENT_APPROVED_SAMPLE")
    nexus_exp = _num((perf.get("approved") or {}).get("net_expectancy_r"))
    if nexus_exp is None or nexus_exp <= 0:
        b.append("APPROVED_EXPECTANCY_NOT_POSITIVE")

    parity = artifact.get("historical_context_parity_complete")
    if parity is None:
        parity = all(
            bool((s.get("historical_context") or {}).get("parity_complete"))
            for s in artifact.get("symbols") or [{}]
        )
    if policy.require_context_parity and parity is not True:
        b.append("HISTORICAL_CONTEXT_PARITY_INCOMPLETE")

    rparity = artifact.get("replay_parity")
    if not isinstance(rparity, dict):
        b.append("REPLAY_PARITY_MISSING")
    else:
        if rparity.get("exit_parity_complete") is not True:
            b.append("EXIT_PARITY_INCOMPLETE")
        if rparity.get("pretrade_parity_complete") is not True:
            b.append("PRETRADE_CONTEXT_PARITY_INCOMPLETE")
        if rparity.get("portfolio_parity_complete") is not True:
            b.append("PORTFOLIO_PARITY_INCOMPLETE")
    manifest = artifact.get("replay_policy_manifest")
    if not isinstance(manifest, dict) or not manifest.get("policy_sha256"):
        b.append("REPLAY_POLICY_MANIFEST_MISSING")
    if policy.require_live_policy_attestation and artifact.get("policy_parity") != "ATTESTED_MATCH":
        b.append("LIVE_POLICY_NOT_ATTESTED")

    symbols = artifact.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        b.append("SYMBOLS_MISSING")
    else:
        if any(s.get("error") for s in symbols):
            b.append("SYMBOLS_UNAVAILABLE")
        days = [_num(s.get("decision_window_days", s.get("history_days")))
                for s in symbols if not s.get("error")]
        if not days or any(d is None or d < policy.min_history_days for d in days):
            b.append("HISTORY_HORIZON_TOO_SHORT")

    folds = cand.get("temporal_folds") or {}
    folds_pos = _num(folds.get("folds_positive_approved_expectancy"))
    if folds_pos is None or folds_pos < policy.min_temporal_folds_positive:
        b.append("TEMPORAL_ROBUSTNESS_INSUFFICIENT")

    # ── CANDIDATE_RESEARCH (signal edge, horizon-aware block authority only) ──
    infer = cand.get("inference") or {}
    horizon = infer.get("outcome_horizon_resolved_executable") or {}
    req = required_block_ms(horizon.get("max_ms"))
    reported_req = _num(infer.get("required_block_ms"))
    if req is not None and (reported_req is None or reported_req < req):
        b.append("AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON")
    appr_lo = _authority(infer.get("approved_expectancy") or {}, "APPROVED_EXPECTANCY", b,
                         required_ms=req, min_blocks=policy.min_independent_blocks)
    if appr_lo is None or appr_lo <= 0:
        b.append("APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE")
    up_lo = _authority(infer.get("uplift_vs_baseline") or {}, "UPLIFT", b,
                       required_ms=req, min_blocks=policy.min_independent_blocks)
    if up_lo is None or up_lo <= 0:
        b.append("UPLIFT_BLOCK_CI_NOT_POSITIVE")
    cens = cand.get("censoring")
    if not isinstance(cens, dict):
        b.append("CENSORING_REPORT_MISSING")
    elif cens.get("censoring_material") is not False:
        b.append("CENSORING_MATERIAL")
    adequacy = cand.get("sample_adequacy") or {}
    n_ind = _num(adequacy.get("independent_blocks_approved"))
    if n_ind is None or n_ind < policy.min_independent_blocks:
        b.append("INSUFFICIENT_INDEPENDENT_BLOCKS")
    eff = ((cand.get("effective_sample") or {}).get("approved")) or {}
    blocks = _num(eff.get("unique_blocks"))
    if blocks is None or blocks < policy.min_approved_unique_blocks:
        b.append("INSUFFICIENT_UNIQUE_TEMPORAL_BLOCKS")
    n_eff = _num(eff.get("effective_n"))
    if n_eff is None or n_eff < policy.min_approved_effective_n:
        b.append("INSUFFICIENT_EFFECTIVE_SAMPLE")

    conc = cand.get("concentration") or {}
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

    stress = cand.get("cost_stress_approved") or {}
    for name in policy.cost_stress_scenarios:
        exp = _num((stress.get(name) or {}).get("net_expectancy_r"))
        if exp is None or exp <= 0:
            b.append(f"COST_STRESS_FAILS_{name.upper()}")

    # ── PORTFOLIO_EXECUTION_REPLAY (executable edge) ──
    port = artifact.get("portfolio_replay")
    if not isinstance(port, dict):
        b.append("PORTFOLIO_REPLAY_MISSING")
    else:
        if port.get("accounting_invariants") != "PASS":
            b.append("PORTFOLIO_ACCOUNTING_INVARIANTS_NOT_PROVEN")
        p_exp = _num(port.get("net_expectancy_r"))
        if p_exp is None or p_exp <= 0:
            b.append("PORTFOLIO_EXPECTANCY_NOT_POSITIVE")
        start, end = _num(port.get("starting_equity")), _num(port.get("ending_equity"))
        if start is None or end is None or end <= start:
            b.append("PORTFOLIO_FINAL_EQUITY_NOT_ABOVE_START")
        realized = _num(port.get("realized_return"))
        if realized is None or realized <= 0:
            b.append("PORTFOLIO_REALIZED_RETURN_NOT_POSITIVE")
        if (port.get("end_state") or {}).get("portfolio_censoring_material") is not False:
            b.append("PORTFOLIO_CENSORING_MATERIAL")
        mdd, limit = _num(port.get("portfolio_max_drawdown")), _num(port.get("research_max_drawdown_limit"))
        if mdd is None or limit is None or mdd > limit:
            b.append("PORTFOLIO_DRAWDOWN_EXCEEDS_RESEARCH_LIMIT")
        # Authority: walk-forward independent calendar folds on the real market
        # timeline. The path bootstrap (spliced timelines) and the trade-level
        # CI are approximate and never read.
        wf = port.get("walk_forward") or {}
        folds_pos, folds_tot = _num(wf.get("folds_positive")), _num(wf.get("folds_total"))
        if folds_pos is None or folds_tot is None or folds_tot < 1:
            b.append("PORTFOLIO_ROBUSTNESS_NOT_ESTIMABLE")
        elif folds_pos < policy.min_walk_forward_folds_positive:
            b.append("PORTFOLIO_WALK_FORWARD_NOT_POSITIVE")
        trades = _num(port.get("total_trades"))
        if trades is None or trades < policy.min_portfolio_trades:
            b.append("INSUFFICIENT_PORTFOLIO_TRADES")
        csym = _num(port.get("contributing_symbols"))
        if csym is None or csym < policy.min_portfolio_contributing_symbols:
            b.append("TOO_FEW_PORTFOLIO_SYMBOLS_CONTRIBUTING")

    method = artifact.get("methodology") or {}
    for flag in ("closed_candles_only", "closed_candle_sentinel_verified", "historical_clock_frozen",
                 "fees_included", "slippage_included"):
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
    return evaluate(data, policy)


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
