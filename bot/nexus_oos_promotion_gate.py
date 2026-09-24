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
  (re-derived by the gate) and that span >= 30 resampling blocks; uplift
  authority lower bound > 0; censoring not material; temporal folds.
* PORTFOLIO_EXECUTION_REPLAY (executable edge): net expectancy > 0, marked
  AND realized final equity > start, no material portfolio censoring,
  walk-forward folds positive (the path bootstrap is non-authoritative), max
  equity drawdown within the research limit, enough trades and contributing
  symbols, accounting invariants proven.
* POLICY CONTENT: research promotion allows LIVE_POLICY_OBSERVATION_PENDING
  or POLICY_CONTENT_MATCH; a mismatched or invalid observation blocks. Content
  match is integrity only, never provenance. LIVE provenance, exact deployment
  SHA, protection evidence and human approval are Stage-C checks
  (``evaluate_live`` / ``bot.live_release_evidence``).
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
    min_resampling_blocks: int = 30
    min_walk_forward_folds_positive: int = 3
    min_history_days: float = 150.0
    require_context_parity: bool = True
    cost_stress_scenarios: tuple[str, ...] = ("fees_plus_50pct", "slippage_x2")


RESEARCH_GATE = "RESEARCH_PROMOTION_GATE"
LIVE_GATE = "LIVE_RELEASE_GATE"
# Policy CONTENT states from the replay (never provenance; see policy_attestation).
POLICY_OBSERVATION_PENDING = "LIVE_POLICY_OBSERVATION_PENDING"
POLICY_CONTENT_MATCH = "POLICY_CONTENT_MATCH"
RESEARCH_ALLOWED_POLICY_STATES = frozenset({POLICY_OBSERVATION_PENDING, POLICY_CONTENT_MATCH})
REAL_ORDER_ENABLEMENT = "HUMAN_ACTION_REQUIRED"


@dataclass
class GateResult:
    promote: bool
    blockers: list[str] = field(default_factory=list)
    exit_code: int = EXIT_BLOCKED
    gate: str = RESEARCH_GATE
    policy_content: str | None = None
    stages: dict = field(default_factory=dict)
    source_authenticated: bool = False

    def to_dict(self) -> dict:
        live = self.gate == LIVE_GATE
        stages = {"RESEARCH_PROMOTION": "PASS" if (self.promote and not live) else "BLOCK",
                  "PRELIVE_EVIDENCE": "BLOCK", "LIVE_RELEASE_PRECONDITIONS": "BLOCK",
                  **self.stages, "REAL_ORDER_ENABLEMENT": REAL_ORDER_ENABLEMENT}
        return {
            "gate": self.gate,
            "verdict": ("PASS" if self.promote else "BLOCK"),
            "blockers": sorted(set(self.blockers)),
            "exit_code": self.exit_code,
            "policy_content": self.policy_content,
            "stages": stages,
            "live_provenance_authenticated": bool(self.source_authenticated),
            # production_ready only from the LIVE gate, only when every Stage-C
            # precondition passed on SOURCE-AUTHENTICATED evidence. Local CLI
            # arguments can never produce it.
            "production_ready": bool(live and self.promote and self.source_authenticated),
            "authorizes_real_trading": False,
            "meaning": ("all automated Stage-C preconditions met on source-authenticated "
                        "evidence; real orders still require the human enablement step" if live
                        else "research/code-merge evidence only; NEVER production readiness"),
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


def block_only_authority(section: dict, *, required_ms: int | None, min_blocks: int,
                         paired: bool = False) -> tuple[float | None, float | None, str]:
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
    valid, long_enough, usable_ivs = [], [], []
    for iv in ivs:
        if not isinstance(iv, dict):
            continue
        ms = _num(iv.get("block_ms"))
        ci = iv.get("ci") or [None, None]
        n_blk = _num(iv.get("resampling_blocks"))
        if ms is None or ms < required_ms:
            continue
        long_enough.append(iv)
        lo, hi = (_num(ci[0]), _num(ci[1])) if len(ci) == 2 else (None, None)
        if n_blk is None or n_blk < min_blocks or lo is None or hi is None:
            continue
        valid.append((lo, hi))
        usable_ivs.append((ms, iv))
    if usable_ivs:
        # Predeclared residual-dependence rule, RECOMPUTED here from the
        # per-block aggregate series (block means); stored ACF values are only
        # cross-checked, never trusted.
        _, longest = max(usable_ivs, key=lambda x: x[0])
        status = residual_dependence_recompute(
            longest, paired=paired, min_blocks=min_blocks,
            reported_estimate=(section or {}).get("delta" if paired else "mean_r"))
        if status is not None:
            return None, None, status
    if valid:
        return min(lo for lo, _ in valid), max(hi for _, hi in valid), "VALID"
    if not long_enough:
        return None, None, "AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON"
    return None, None, "INSUFFICIENT_RESAMPLING_BLOCKS"


AGGREGATE_POINT_TOLERANCE = 1e-8     # numerical only (aggregates are rounded to 1e-12)


def _influence_series_error(series, *, diff: bool, expected_a_blocks) -> str | None:
    """Structural checks on influence-score aggregates. None when valid."""
    pre = "UPLIFT_RESIDUAL_SERIES" if diff else "RESIDUAL_DEPENDENCE_SERIES"
    if not isinstance(series, dict):
        return pre + "_MISSING"
    keys = (("block_ids", "a_sum", "a_count", "b_sum", "b_count") if diff
            else ("block_ids", "sum_r", "count"))
    cols = [series.get(k) for k in keys]
    if not all(isinstance(c, list) for c in cols):
        return pre + "_MISSING"
    if len({len(c) for c in cols}) != 1 or not cols[0]:
        return pre + "_INCONSISTENT"
    ids = cols[0]
    if any(isinstance(b, bool) or not isinstance(b, int) for b in ids):
        return pre + "_INCONSISTENT"
    if any(b2 <= b1 for b1, b2 in zip(ids, ids[1:])):          # strictly increasing => unique
        return pre + "_INCONSISTENT"
    pairs = list(zip(cols[1::2], cols[2::2]))                   # (sums, counts) per population
    for sums, cnts in pairs:
        for c in cnts:
            if isinstance(c, bool) or not isinstance(c, int) or c < 0:
                return pre + "_INCONSISTENT"
        for v in sums:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return pre + "_INCONSISTENT"
        if any(c == 0 and v != 0 for v, c in zip(sums, cnts)) or sum(cnts) <= 0:
            return pre + "_INCONSISTENT"
    # Resampling blocks = blocks with >= 1 row of the (first) selected population.
    a_cnt = pairs[0][1]
    if expected_a_blocks is None or sum(1 for c in a_cnt if c >= 1) != expected_a_blocks:
        return pre + "_INCONSISTENT"
    return None


def residual_dependence_recompute(iv: dict, *, paired: bool = False, min_blocks: int = 30,
                                  reported_estimate=None) -> str | None:
    """Independently recompute the estimator-aligned residual dependence.

    The series kind must be MEAN_INFLUENCE_SCORE_V1 (single mean) or
    DIFF_MEAN_INFLUENCE_SCORE_V1 (difference, e.g. uplift); equal-weight
    BLOCK_MEAN / PAIRED_BLOCK_DELTA series are diagnostic only and rejected.
    From the compact aggregates the gate recomputes mu, mean counts, every
    block influence score psi(t) and the CALENDAR lag-1..3 ACF (pairs only when
    block ids differ by exactly k). It also checks that the aggregates reproduce
    the reported point estimate. Stored scores/ACF are never trusted.
    Returns None when there is no significant residual dependence.
    """
    from bot import nexus_oos_inference as inf
    rd = iv.get("residual_dependence")
    kind = inf.SERIES_KIND_DIFF if paired else inf.SERIES_KIND_MEAN
    pre = "UPLIFT_RESIDUAL_SERIES" if paired else "RESIDUAL_DEPENDENCE_SERIES"
    if not isinstance(rd, dict):
        return pre + "_MISSING"
    if rd.get("residual_series_kind") != kind:
        return pre + "_KIND_INVALID"
    series = rd.get("series")
    n_blk = _num(iv.get("resampling_blocks"))
    err = _influence_series_error(series, diff=paired,
                                  expected_a_blocks=int(n_blk) if n_blk is not None else None)
    if err is not None:
        return err
    sc = inf.influence_scores(series, diff=paired)
    if sc is None:
        return pre + "_INCONSISTENT"
    ids, psi, point = sc
    rep = _num(reported_estimate)
    if rep is None or abs(point - rep) > AGGREGATE_POINT_TOLERANCE:
        return "RESIDUAL_AGGREGATES_POINT_ESTIMATE_MISMATCH"
    res = inf.acf_from_block_series(ids, psi)
    reported = rd.get("acf") or {}
    for k, v in res["acf"].items():
        r = reported.get(k)
        if (v is None) != (r is None) or (v is not None and (_num(r) is None or abs(_num(r) - v) > 1e-9)):
            return pre + "_INCONSISTENT"
    if not res["estimable"]:
        return inf.DIFF_RESIDUAL_NOT_ESTIMABLE if paired else inf.RESIDUAL_NOT_ESTIMABLE
    if res["significant"]:
        return (inf.DIFF_RESIDUAL_DEPENDENCE if paired
                else "RESIDUAL_DEPENDENCE_AT_LONGEST_USABLE_BLOCK")
    return None


def _authority(section: dict, name: str, b: list, *, required_ms, min_blocks,
               paired: bool = False) -> float | None:
    lo, _, status = block_only_authority(section, required_ms=required_ms, min_blocks=min_blocks,
                                         paired=paired)
    if status != "VALID":
        b.append(status)
    reported = _num((section or {}).get("authority_ci_low"))
    if lo is not None and (reported is None or abs(lo - reported) > 1e-9):
        b.append(f"{name}_AUTHORITY_CI_INCONSISTENT")
    if lo is None and reported is not None:
        b.append(f"{name}_AUTHORITY_CI_INCONSISTENT")
    return lo


def folds_independent(section: dict, *, required_ms: int | None) -> tuple[bool, str | None]:
    """Recompute fold independence from the reported dates (never trust flags).

    Each fold's decision window must be followed by an outcome-completion
    window (>= the required outcome horizon) plus an embargo (>= that horizon),
    and the market time it used must end before the next fold's first decision.
    """
    folds = (section or {}).get("folds")
    if not isinstance(folds, list) or not folds:
        return False, "FOLDS_MISSING"
    emb, req_rep = _num(section.get("fold_embargo_ms")), _num(section.get("fold_required_horizon_ms"))
    if emb is None or req_rep is None or emb < req_rep:
        return False, "FOLD_EMBARGO_BELOW_REQUIRED_HORIZON"
    if required_ms is not None and (emb < required_ms or req_rep < required_ms):
        return False, "FOLD_EMBARGO_BELOW_GATE_HORIZON"
    gap = emb + req_rep
    for a, b_ in zip(folds, folds[1:]):
        used = _num(a.get("max_market_ts_used"))
        nxt = _num(b_.get("decision_start_ts"))
        end = _num(a.get("decision_end_ts"))
        if used is None or nxt is None or end is None or used >= nxt or nxt - end < gap:
            return False, "FOLD_MARKET_TIME_OVERLAP"
    if section.get("folds_overlap_free") is not True:
        return False, "FOLDS_REPORTED_NOT_OVERLAP_FREE"
    return True, None


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
    parity_state = artifact.get("policy_parity")
    if parity_state not in RESEARCH_ALLOWED_POLICY_STATES:
        # Mismatched or invalid policy CONTENT blocks even research promotion.
        # Research promotion never depends on LIVE provenance.
        b.append("POLICY_CONTENT_MISMATCH_OR_INVALID")

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

    # ── CANDIDATE_RESEARCH (signal edge, horizon-aware block authority only) ──
    infer = cand.get("inference") or {}
    horizon = infer.get("outcome_horizon_resolved_executable") or {}
    req = required_block_ms(horizon.get("max_ms"))
    reported_req = _num(infer.get("required_block_ms"))
    if req is not None and (reported_req is None or reported_req < req):
        b.append("AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON")

    folds = cand.get("temporal_folds") or {}
    if folds.get("status") not in (None, "OK"):
        b.append("INSUFFICIENT_INDEPENDENT_TEMPORAL_FOLDS")
    ok, why = folds_independent(folds, required_ms=req)
    if not ok:
        b.append("TEMPORAL_FOLDS_NOT_INDEPENDENT")
    folds_pos = _num(folds.get("folds_positive_approved_expectancy"))
    if not ok or folds_pos is None or folds_pos < policy.min_temporal_folds_positive:
        b.append("TEMPORAL_ROBUSTNESS_INSUFFICIENT")
    appr_lo = _authority(infer.get("approved_expectancy") or {}, "APPROVED_EXPECTANCY", b,
                         required_ms=req, min_blocks=policy.min_resampling_blocks)
    if appr_lo is None or appr_lo <= 0:
        b.append("APPROVED_EXPECTANCY_BLOCK_CI_NOT_POSITIVE")
    up_lo = _authority(infer.get("uplift_vs_baseline") or {}, "UPLIFT", b,
                       required_ms=req, min_blocks=policy.min_resampling_blocks, paired=True)
    if up_lo is None or up_lo <= 0:
        b.append("UPLIFT_BLOCK_CI_NOT_POSITIVE")
    cens = cand.get("censoring")
    if not isinstance(cens, dict):
        b.append("CENSORING_REPORT_MISSING")
    elif cens.get("censoring_material") is not False:
        b.append("CENSORING_MATERIAL")
    adequacy = cand.get("sample_adequacy") or {}
    n_ind = _num(adequacy.get("resampling_blocks_approved"))
    if n_ind is None or n_ind < policy.min_resampling_blocks:
        b.append("INSUFFICIENT_RESAMPLING_BLOCKS")
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
        if wf.get("status") not in (None, "OK"):
            b.append("INSUFFICIENT_INDEPENDENT_PORTFOLIO_PERIODS")
        wf_ok, _ = folds_independent(wf, required_ms=req)
        if not wf_ok:
            b.append("PORTFOLIO_FOLDS_NOT_INDEPENDENT")
        if wf.get("fold_account_state") != "RESET_FOR_REGIME_ROBUSTNESS":
            b.append("PORTFOLIO_FOLD_ACCOUNT_STATE_UNDECLARED")
        if folds_pos is None or folds_tot is None or folds_tot < 1:
            b.append("PORTFOLIO_ROBUSTNESS_NOT_ESTIMABLE")
        elif not wf_ok or folds_pos < policy.min_walk_forward_folds_positive:
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
    return GateResult(not b, b, EXIT_PROMOTE if not b else EXIT_BLOCKED,
                      gate=RESEARCH_GATE, policy_content=parity_state)


def evaluate_live(local_artifact: dict | None, evidence=None, *, sources=None, now=None,
                  policy: GatePolicy = GatePolicy(),
                  release_authority_kind: str = "NEXUS_ONLY") -> GateResult:
    """LIVE_RELEASE_GATE (stage C). Never deploys, never mutates anything.

    The research gate is evaluated ONLY on the OOS artifact fetched from the
    trusted CI provider (exact-SHA, successful "NEXUS Real OOS Replay" run,
    digest-verified). ``local_artifact`` is a caller-supplied copy used for
    comparison/display only; it never decides anything. All other Stage-C
    claims are confirmed by ``bot.live_release_evidence.verify``. Without
    trusted sources the verdict is BLOCK. This function is a reference
    implementation; the authoritative Stage-C verifier runs from the protected
    release environment (RELEASE_EVIDENCE.md §7), never from the candidate.
    """
    from dataclasses import replace as _replace
    from bot import live_release_evidence as lre
    ev = lre.verify(evidence, local_artifact=local_artifact if isinstance(local_artifact, dict) else None,
                    sources=sources, now=now, release_authority_kind=release_authority_kind)
    comp = ev["components"]
    trusted = ev["trusted_artifact"]
    if trusted is not None:
        res = evaluate(trusted, _replace(policy, require_context_parity=True))
    else:
        res = GateResult(False, ["TRUSTED_RESEARCH_ARTIFACT_UNAVAILABLE"], EXIT_BLOCKED)
    b = list(res.blockers) + list(ev["blockers"])
    prelive = all(comp[k] == "PASS" for k in (
        "RESEARCH_ARTIFACT_AUTHENTICATED", "POLICY_CONTENT_MATCH", "LIVE_PROVENANCE_AUTHENTICATED",
        "EXACT_DEPLOYMENT_SHA_MATCH", "CI_EVIDENCE", "RELEASE_VERIFIER_TRUSTED"))
    ai_verdict = (ev.get("ai_identity") or {}).get("verdict", "BLOCK")
    if release_authority_kind == "AI_LIVE":
        # AI LIVE: the AI identity binding is PART of the verdict (never PASS
        # with AI_IDENTITY = BLOCK). NEXUS_ONLY keeps its own semantics.
        if ai_verdict != "PASS":
            b.append("AI_IDENTITY_NOT_PROVEN")
        # Historical AI evidence is AI_HOOK_EDGE only while effective-execution
        # parity is INCOMPLETE; it can never authorize AI LIVE on its own.
        from bot.ai import ai_gate
        ai_live = ai_gate.evaluate(trusted or {})["live_historical_promotion"]
        if ai_live["verdict"] != "PASS":
            b.append("AI_LIVE_PROMOTION_EVIDENCE_NOT_PROVEN")
            b.extend(x for x in ai_live["blockers"] if x == "AI_EFFECTIVE_EXECUTION_PARITY_INCOMPLETE")
    elif release_authority_kind != "NEXUS_ONLY":
        b.append("RELEASE_AUTHORITY_KIND_UNKNOWN")
    live_ok = (prelive and res.promote and all(v == "PASS" for v in comp.values())
               and (release_authority_kind != "AI_LIVE"
                    or (ai_verdict == "PASS" and "AI_LIVE_PROMOTION_EVIDENCE_NOT_PROVEN" not in b)))
    b = sorted(set(b))
    code = EXIT_PROMOTE if (live_ok and not b) else EXIT_BLOCKED
    stages = {"RESEARCH_PROMOTION": "PASS" if res.promote else "BLOCK",
              "PRELIVE_EVIDENCE": "PASS" if prelive else "BLOCK",
              "LIVE_RELEASE_PRECONDITIONS": "PASS" if (live_ok and not b) else "BLOCK",
              "evidence_components": comp,
              "release_verifier_version": ev["release_verifier_version"],
              "release_verifier_sha": ev["release_verifier_sha"],
              # AI_EXECUTION_MODE=LIVE additionally requires this == PASS
              # (bot.ai.runtime.authorize_ai_live); NEXUS-only LIVE ignores it.
              "release_authority_kind": release_authority_kind,
              "AI_IDENTITY": ai_verdict,
              "ai_identity": ev.get("ai_identity")}
    return GateResult(bool(live_ok and not b), b, code, gate=LIVE_GATE,
                      policy_content=(trusted or {}).get("policy_parity"),
                      stages=stages, source_authenticated=ev["source_authenticated"])


def _load(path: str | Path):
    p = Path(path)
    if not p.is_file():
        return None, GateResult(False, ["ARTIFACT_MISSING"], EXIT_MISSING)
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, GateResult(False, ["ARTIFACT_CORRUPT"], EXIT_CORRUPT)


def evaluate_path(path: str | Path, policy: GatePolicy = GatePolicy()) -> GateResult:
    data, err = _load(path)
    return err if err is not None else evaluate(data, policy)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("artifact")
    parser.add_argument("--gate", choices=("research", "live"), default="research",
                        help="research = RESEARCH_PROMOTION_GATE (code-merge evidence); "
                             "live = LIVE_RELEASE_GATE (stage C, before real orders).")
    parser.add_argument(
        "--allow-incomplete-context-parity", action="store_true",
        help="Research gate only. Never passed by the PR workflow; ignored by the live gate.",
    )
    parser.add_argument("--release-evidence", default=None,
                        help="BGX_LIVE_RELEASE_EVIDENCE_V1 JSON (live gate). Its claims are "
                             "verified only against trusted read-only sources; no such source "
                             "is configured in this CLI, so the live gate BLOCKS.")
    args = parser.parse_args(argv)
    if args.gate == "live":
        data, err = _load(args.artifact)
        evidence = None
        if args.release_evidence:
            try:
                evidence = Path(args.release_evidence).read_text(encoding="utf-8")
            except OSError:
                evidence = "UNREADABLE"
        # No trusted control-plane provider exists in this repository: the CLI
        # can never authenticate provenance, so it can never pass Stage C.
        result = err if err is not None else evaluate_live(data, evidence, sources=None)
        if err is not None:
            result.gate = LIVE_GATE
    else:
        policy = GatePolicy(require_context_parity=not args.allow_incomplete_context_parity)
        result = evaluate_path(args.artifact, policy)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
