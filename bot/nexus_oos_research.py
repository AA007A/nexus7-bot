"""Pure OOS research statistics for NEXUS replay artifacts.

Research only: no network, no exchange access, no runtime mutation. Every
function takes plain rows (dicts) produced by ``nexus_oos_real_replay`` and
returns JSON-serializable dicts.

Conventions
-----------
* A "row" is one strategy candidate evaluated at a closed-candle decision
  timestamp. ``r`` is its net R multiple under the current cost model (fees,
  slippage, funding included). ``approved`` is the NEXUS decision at the
  production threshold.
* ``candidate_sequence_drawdown_r`` is the drawdown of the cumulative R of
  candidates in decision order. Candidates overlap in time, so this is NOT an
  account drawdown; only ``nexus_oos_portfolio_replay`` reports account/equity
  drawdown (``portfolio_max_drawdown``).
* Sharpe/Sortino-like ratios here are PER-TRADE ratios (mean R / std R over
  trades). They are NOT annualized time-series Sharpe ratios and must not be
  compared to one.
* Bootstrap intervals are percentile intervals with a fixed seed so artifacts
  are reproducible.
* Tail metrics (1st percentile, CVaR) are reported only when the sample is
  large enough for the tail to contain several observations; otherwise None.
"""
from __future__ import annotations

from collections import defaultdict
import math
import random
from typing import Callable, Iterable, Sequence

BOOTSTRAP_SAMPLES = 2000
SEED = 7
MIN_TAIL_SAMPLE = 40            # CVaR 5% needs >= 2 observations in the tail
MIN_P1_SAMPLE = 200             # 1st percentile needs >= 2 observations
BREAKEVEN_EPS_R = 0.02          # |R| below this is a breakeven trade

REQUIRED_REGIMES = (
    "TRENDING_BULL", "TRENDING_BEAR", "RANGE", "BREAKOUT", "BREAKDOWN",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "CHOPPY", "EXTREME_EVENT", "UNKNOWN",
)


# ── basic statistics ─────────────────────────────────────────────────────────
def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _percentile(sorted_xs: Sequence[float], q: float) -> float | None:
    if not sorted_xs:
        return None
    idx = min(len(sorted_xs) - 1, max(0, int(math.floor(q * (len(sorted_xs) - 1)))))
    return sorted_xs[idx]


def bootstrap_mean_ci(xs: Sequence[float], *, samples: int = BOOTSTRAP_SAMPLES,
                      seed: int = SEED) -> tuple[float | None, float | None]:
    if len(xs) < 2:
        return None, None
    rng = random.Random(seed)
    n = len(xs)
    means = []
    for _ in range(samples):
        means.append(sum(xs[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return means[int(0.025 * samples)], means[min(samples - 1, int(0.975 * samples))]


def _streaks(rs: Sequence[float]) -> tuple[int, int]:
    best_win = best_loss = cur_win = cur_loss = 0
    for r in rs:
        if r > BREAKEVEN_EPS_R:
            cur_win += 1
            cur_loss = 0
        elif r < -BREAKEVEN_EPS_R:
            cur_loss += 1
            cur_win = 0
        else:
            cur_win = cur_loss = 0
        best_win = max(best_win, cur_win)
        best_loss = max(best_loss, cur_loss)
    return best_win, best_loss


def _max_drawdown_r(rs: Sequence[float]) -> float:
    peak = equity = 0.0
    mdd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)
    return mdd


def performance(rows: Sequence[dict], *, with_ci: bool = True) -> dict:
    """Full performance block for rows in chronological order."""
    rows = sorted(rows, key=lambda r: r["ts"])
    rs = [float(r["r"]) for r in rows]
    n = len(rs)
    out: dict = {"trades": n}
    if n == 0:
        return out
    wins = [r for r in rs if r > BREAKEVEN_EPS_R]
    losses = [r for r in rs if r < -BREAKEVEN_EPS_R]
    srt = sorted(rs)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1) if n > 1 else 0.0
    downside = [min(0.0, r) for r in rs]
    dvar = sum(d * d for d in downside) / n
    win_streak, loss_streak = _streaks(rs)

    def _sum(key):
        vals = [r.get(key) for r in rows]
        return sum(float(v) for v in vals if v is not None) if any(v is not None for v in vals) else None

    out.update({
        "win_rate": len(wins) / n,
        "loss_rate": len(losses) / n,
        "breakeven_rate": (n - len(wins) - len(losses)) / n,
        "avg_r": mean,
        "median_r": _percentile(srt, 0.5),
        "avg_winner_r": _mean(wins),
        "avg_loser_r": _mean(losses),
        "payoff_ratio": (_mean(wins) / abs(_mean(losses))) if wins and losses else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "gross_expectancy_r": (_sum("gross_r") / n) if _sum("gross_r") is not None else None,
        "net_expectancy_r": mean,
        "total_net_r": sum(rs),
        "total_fees_r": _sum("fees_r"),
        "total_slippage_r": _sum("slippage_r"),
        "funding_contribution_r": _sum("funding_r"),
        "candidate_sequence_drawdown_r": _max_drawdown_r(rs),
        "longest_winning_streak": win_streak,
        "longest_losing_streak": loss_streak,
        "p05_r": _percentile(srt, 0.05) if n >= 20 else None,
        "p01_r": _percentile(srt, 0.01) if n >= MIN_P1_SAMPLE else None,
        "cvar05_r": (_mean(srt[:max(2, int(0.05 * n))]) if n >= MIN_TAIL_SAMPLE else None),
        "per_trade_sharpe": (mean / math.sqrt(var)) if var > 0 else None,
        "per_trade_sortino": (mean / math.sqrt(dvar)) if dvar > 0 else None,
        "ratio_convention": "PER_TRADE_NOT_ANNUALIZED",
    })
    if with_ci:
        lo, hi = bootstrap_mean_ci(rs)
        out["expectancy_ci_low_r"], out["expectancy_ci_high_r"] = lo, hi
    return out


def compact(rows: Sequence[dict]) -> dict:
    """Small metric set for segments / sweeps."""
    rs = [float(r["r"]) for r in sorted(rows, key=lambda r: r["ts"])]
    n = len(rs)
    if n == 0:
        return {"trades": 0}
    wins = [r for r in rs if r > BREAKEVEN_EPS_R]
    losses = [-r for r in rs if r < -BREAKEVEN_EPS_R]
    lo, hi = bootstrap_mean_ci(rs, samples=1000)
    return {
        "trades": n,
        "net_expectancy_r": sum(rs) / n,
        "total_net_r": sum(rs),
        "win_rate": len(wins) / n,
        "profit_factor": (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else None,
        "candidate_sequence_drawdown_r": _max_drawdown_r(rs),
        "expectancy_ci_low_r": lo,
        "expectancy_ci_high_r": hi,
    }


# ── feature buckets / regime classifier (research labels only) ──────────────
def _ema(xs: Sequence[float], n: int) -> list[float]:
    k = 2.0 / (n + 1)
    out, e = [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def _true_ranges(h, l, c):
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                            for i in range(1, len(c))]


def atr_pct(h, l, c, n: int = 14) -> float | None:
    if len(c) < n + 1 or c[-1] <= 0:
        return None
    tr = _true_ranges(h, l, c)[-n:]
    return sum(tr) / n / c[-1]


def adx(h, l, c, n: int = 14) -> float | None:
    if len(c) < 2 * n + 1:
        return None
    plus, minus = [], []
    for i in range(1, len(c)):
        up, down = h[i] - h[i - 1], l[i - 1] - l[i]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    tr = _true_ranges(h, l, c)[1:]
    dx = []
    for i in range(n - 1, len(tr)):
        trs = sum(tr[i - n + 1:i + 1])
        if trs <= 0:
            continue
        pdi = 100 * sum(plus[i - n + 1:i + 1]) / trs
        mdi = 100 * sum(minus[i - n + 1:i + 1]) / trs
        if pdi + mdi > 0:
            dx.append(100 * abs(pdi - mdi) / (pdi + mdi))
    return sum(dx[-n:]) / min(n, len(dx)) if dx else None


def choppiness(h, l, c, n: int = 14) -> float | None:
    if len(c) < n + 1:
        return None
    tr = _true_ranges(h, l, c)[-n:]
    rng = max(h[-n:]) - min(l[-n:])
    if rng <= 0 or sum(tr) <= 0:
        return None
    return 100 * math.log10(sum(tr) / rng) / math.log10(n)


def classify_regime(window_1h: Sequence[dict]) -> str:
    """Deterministic research regime label from CLOSED 1h candles only.

    Priority: EXTREME_EVENT > BREAKOUT/BREAKDOWN > HIGH/LOW_VOLATILITY >
    TRENDING_BULL/BEAR > CHOPPY > RANGE. UNKNOWN when history is too short.
    This is a diagnostic label, not the production regime detector.
    """
    if len(window_1h) < 40:
        return "UNKNOWN"
    h = [float(k["h"]) for k in window_1h]
    l = [float(k["l"]) for k in window_1h]
    c = [float(k["c"]) for k in window_1h]
    a_now = atr_pct(h, l, c)
    atrs = [atr_pct(h[:i], l[:i], c[:i]) for i in range(20, len(c) + 1)]
    atrs = sorted(a for a in atrs if a)
    if a_now is None or not atrs:
        return "UNKNOWN"
    med = atrs[len(atrs) // 2]
    last_ret = abs(c[-1] / c[-2] - 1.0) if c[-2] > 0 else 0.0
    if last_ret > 4.0 * a_now:
        return "EXTREME_EVENT"
    if c[-1] > max(h[-21:-1]):
        return "BREAKOUT"
    if c[-1] < min(l[-21:-1]):
        return "BREAKDOWN"
    if a_now > 1.5 * med:
        return "HIGH_VOLATILITY"
    if a_now < 0.6 * med:
        return "LOW_VOLATILITY"
    ax = adx(h, l, c)
    e20, e50 = _ema(c, 20)[-1], _ema(c, 50 if len(c) >= 50 else len(c))[-1]
    if ax is not None and ax >= 25:
        return "TRENDING_BULL" if e20 > e50 else "TRENDING_BEAR"
    ch = choppiness(h, l, c)
    if ch is not None and ch > 61.8:
        return "CHOPPY"
    return "RANGE"


def bucket(value, edges: Sequence[float], labels: Sequence[str] | None = None) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "UNKNOWN"
    for i, edge in enumerate(edges):
        if value < edge:
            return labels[i] if labels else f"<{edge:g}"
    return labels[len(edges)] if labels else f">={edges[-1]:g}"


SEGMENT_KEYS = (
    "symbol", "production_regime", "research_regime", "direction", "entry_type", "score_bucket",
    "confidence_bucket", "utc_hour_bucket", "volatility_bucket", "adx_bucket",
    "volume_bucket",
)


def segments(rows: Sequence[dict]) -> dict:
    out: dict = {}
    for key in SEGMENT_KEYS:
        groups: dict = defaultdict(list)
        for r in rows:
            groups[str(r.get(key, "UNKNOWN"))].append(r)
        if key == "research_regime":
            for label in REQUIRED_REGIMES:
                groups.setdefault(label, [])
        out[key] = {k: compact(v) for k, v in sorted(groups.items())}
    return out


# ── concentration ────────────────────────────────────────────────────────────
def concentration(rows: Sequence[dict], key: str) -> dict:
    """Share of total positive net R contributed by the largest group."""
    totals: dict = defaultdict(float)
    for r in rows:
        totals[str(r.get(key))] += float(r["r"])
    positive = {k: v for k, v in totals.items() if v > 0}
    pos_sum = sum(positive.values())
    top = max(positive.items(), key=lambda kv: kv[1]) if positive else (None, 0.0)
    return {
        "groups": len(totals),
        "groups_positive": len(positive),
        "top_group": top[0],
        "top_share_of_positive_r": (top[1] / pos_sum) if pos_sum > 0 else None,
        "totals_r": dict(sorted(totals.items())),
    }


# ── chronological split and threshold research ───────────────────────────────
def chronological_split(rows: Sequence[dict], fractions=(0.5, 0.25, 0.25)) -> dict:
    """Legacy row-order split WITHOUT purge/embargo. Not used for any decision;
    use ``nexus_oos_inference.purged_split``."""
    ordered = sorted(rows, key=lambda r: r["ts"])
    n = len(ordered)
    a = int(n * fractions[0])
    b = int(n * (fractions[0] + fractions[1]))
    return {"train": ordered[:a], "validation": ordered[a:b], "test": ordered[b:]}


def approved_at(row: dict, threshold: float) -> bool:
    """Approval at an alternative NEXUS threshold.

    ``gates_passed`` means every NEXUS veto other than the score threshold
    passed (decide() evaluated with a non-binding threshold). CHOPPY raises the
    threshold by 15 exactly as ``nexus_ai.decide`` does.
    """
    if not row.get("gates_passed") or row.get("nexus_score") is None:
        return False
    bump = 15.0 if row.get("nexus_regime") == "CHOPPY" else 0.0
    return float(row["nexus_score"]) >= float(threshold) + bump


def threshold_research(rows: Sequence[dict], thresholds: Sequence[float],
                       runtime_threshold: float, *, min_trades: int = 30,
                       min_blocks: int = 10, required_ms: int | None = None) -> dict:
    """TRAIN discovers, VALIDATION confirms, FINAL TEST is reported exactly once.

    Splits are purged and embargoed (``nexus_oos_inference.purged_split``).
    Selection (TRAIN only): the threshold with the highest dependence-aware
    (block-bootstrap authority) lower bound among thresholds with enough trades
    and blocks. A selection is a stable plateau only if both neighbours also
    have a positive TRAIN authority lower bound. VALIDATION must confirm with a
    positive authority lower bound. FINAL TEST is evaluated only for the single
    selected threshold; if it fails, the experiment is reported FAILED and no
    other threshold is selected. The runtime threshold is never changed.
    """
    from bot import nexus_oos_inference as inf

    rows = [r for r in rows if inf._is_resolved(r) and r.get("r") is not None] + \
        [r for r in rows if not inf._is_resolved(r)]
    req = int(required_ms) if required_ms is not None else inf.required_block_ms(rows)
    split = inf.purged_split(rows)
    for name in ("train", "validation", "test"):
        split[name] = [r for r in split[name] if inf._is_resolved(r) and r.get("r") is not None]
    parts = {k: split[k] for k in ("train", "validation")}
    table = []
    for t in thresholds:
        entry = {"threshold": t}
        for name, part in parts.items():
            sel_fn = (lambda r, _t=t: approved_at(r, _t))
            sel = [r for r in part if sel_fn(r)]
            m = compact(sel)
            m["frequency"] = (len(sel) / len(part)) if part else None
            m["blocks"] = len({inf.block_id(r["ts"]) for r in sel})
            dep = inf.dependence_aware_mean(part, sel_fn, samples=1000, required_ms=req) if sel else {}
            m["authority_ci_low_r"] = dep.get("authority_ci_low")
            m["authority_ci_high_r"] = dep.get("authority_ci_high")
            m["block24h_ci"] = dep.get("block24h_ci")
            m["symbol_concentration"] = concentration(sel, "symbol")["top_share_of_positive_r"] if sel else None
            m["regime_concentration"] = concentration(sel, "production_regime")["top_share_of_positive_r"] if sel else None
            entry[name] = m
        table.append(entry)

    def train_lo(e):
        tr = e["train"]
        if tr.get("trades", 0) < min_trades or tr.get("blocks", 0) < min_blocks:
            return None
        return tr.get("authority_ci_low_r")

    eligible = [e for e in table if train_lo(e) is not None]
    selected = max(eligible, key=train_lo) if eligible else None
    stable = False
    validation_ok = False
    final_test = None
    status = "NO_ELIGIBLE_THRESHOLD"
    if selected is not None:
        i = table.index(selected)
        neighbours = [table[j] for j in (i - 1, i + 1) if 0 <= j < len(table)]
        stable = (len(neighbours) == 2 and all((train_lo(nb) or -1) > 0 for nb in neighbours)
                  and (train_lo(selected) or -1) > 0)
        validation_ok = (selected["validation"].get("authority_ci_low_r") or -1) > 0
        if not validation_ok:
            status = "NOT_CONFIRMED_ON_VALIDATION"
        else:
            # FINAL TEST: consulted exactly once, for the selected threshold only.
            t = selected["threshold"]
            test_rows = split["test"]
            sel = [r for r in test_rows if approved_at(r, t)]
            final_test = compact(sel)
            dep = inf.dependence_aware_mean(test_rows, lambda r: approved_at(r, t), samples=1000,
                                            required_ms=req) if sel else {}
            final_test["authority_ci_low_r"] = dep.get("authority_ci_low")
            final_test["authority_ci_high_r"] = dep.get("authority_ci_high")
            status = ("CONFIRMED_ON_FINAL_TEST" if (final_test.get("authority_ci_low_r") or -1) > 0
                      else "EXPERIMENT_FAILED_ON_FINAL_TEST")
    return {
        "split": inf.split_summary(split),
        "split_method": "PURGED_EMBARGOED_TEMPORAL_50_25_25",
        "runtime_threshold": runtime_threshold,
        "runtime_threshold_changed": False,
        "selection_rule": ("max TRAIN block-bootstrap authority lower CI (n>=%d, blocks>=%d); "
                           "VALIDATION confirms; FINAL TEST consulted once" % (min_trades, min_blocks)),
        "selected_threshold": selected["threshold"] if selected else None,
        "selected_is_stable_plateau": stable,
        "validation_confirmed": validation_ok,
        "final_test": final_test,
        "status": status,
        "table": table,
    }


# ── ablation ─────────────────────────────────────────────────────────────────
MIN_ABLATION_TRADES = 30

def paired_ablation(rows: Sequence[dict], variant_key: str, *,
                    samples: int = BOOTSTRAP_SAMPLES, seed: int = SEED,
                    required_ms: int | None = None) -> dict:
    """FULL approved set vs variant approved set on the SAME candidate population.

    Δ = full − variant (positive: the removed component adds expectancy).
    IID and 24h/48h/72h UTC-block bootstrap intervals are reported; the verdict
    uses only the block-only authority interval (IID is diagnostic).
    """
    from bot import nexus_oos_inference as inf

    full_sel = lambda r: bool(r.get("approved"))  # noqa: E731
    var_sel = lambda r: bool((r.get("variants") or {}).get(variant_key))  # noqa: E731
    full = [r for r in rows if full_sel(r)]
    var = [r for r in rows if var_sel(r)]
    fm, vm = compact(full), compact(var)
    dep = inf.dependence_aware_diff(rows, full_sel, var_sel, samples=samples, required_ms=required_ms)
    lo, hi = dep["authority_ci_low"], dep["authority_ci_high"]
    if (fm.get("trades", 0) < MIN_ABLATION_TRADES or vm.get("trades", 0) < MIN_ABLATION_TRADES
            or lo is None or hi is None):
        verdict = "INSUFFICIENT_EVIDENCE"
    elif lo > 0:
        verdict = "COMPONENT_ADDS_EXPECTANCY"
    elif hi < 0:
        verdict = "COMPONENT_HURTS_EXPECTANCY"
    else:
        verdict = "NO_ROBUST_DIFFERENCE"
    return {
        "full": fm, "variant": vm,
        "delta_expectancy_r": dep["delta"],
        "delta_iid_ci": dep["iid_ci"],
        "delta_block24h_ci": dep["block24h_ci"],
        "delta_block48h_ci": dep["block48h_ci"],
        "delta_block72h_ci": dep["block72h_ci"],
        "delta_block_intervals": dep["block_intervals"],
        "delta_authority_status": dep["authority_status"],
        "delta_iid_role": "DIAGNOSTIC_ONLY",
        "delta_ci_low_r": lo, "delta_ci_high_r": hi,
        "delta_profit_factor": (
            (fm.get("profit_factor") or 0) - (vm.get("profit_factor") or 0)
            if fm.get("profit_factor") is not None and vm.get("profit_factor") is not None else None),
        "delta_candidate_sequence_drawdown_r": (
            fm.get("candidate_sequence_drawdown_r", 0) - vm.get("candidate_sequence_drawdown_r", 0)
            if fm.get("trades") and vm.get("trades") else None),
        "delta_approvals": fm.get("trades", 0) - vm.get("trades", 0),
        "verdict": verdict,
        "verdict_basis": "DEPENDENCE_AWARE_BLOCK_BOOTSTRAP",
    }


# ── cost stress ──────────────────────────────────────────────────────────────
COST_SCENARIOS = {
    "current": (1.0, 1.0),
    "fees_plus_25pct": (1.25, 1.0),
    "fees_plus_50pct": (1.5, 1.0),
    "slippage_x1_5": (1.0, 1.5),
    "slippage_x2": (1.0, 2.0),
    "combined_adverse": (1.5, 2.0),
}


def cost_stress(rows: Sequence[dict]) -> dict:
    out = {}
    for name in COST_SCENARIOS:
        rs = [{"ts": r["ts"], "r": r["cost_r"][name]} for r in rows if name in (r.get("cost_r") or {})]
        out[name] = compact(rs)
    return out


def break_even_multiplier(expectancy_at: Callable[[float], float], *,
                          lo: float = 0.0, hi: float = 5.0, iters: int = 30) -> dict:
    """Combined cost multiplier (fees and slippage scaled together) at which
    expectancy crosses zero. Returns bounds when no crossing exists."""
    f_lo, f_hi = expectancy_at(lo), expectancy_at(hi)
    if f_lo <= 0:
        return {"multiplier": None, "note": "NEGATIVE_EVEN_AT_ZERO_COST", "expectancy_at_zero_cost": f_lo}
    if f_hi > 0:
        return {"multiplier": None, "note": f"POSITIVE_BEYOND_{hi:g}X_COST"}
    for _ in range(iters):
        mid = (lo + hi) / 2
        if expectancy_at(mid) > 0:
            lo = mid
        else:
            hi = mid
    return {"multiplier": (lo + hi) / 2, "note": "CROSSING_FOUND"}


# ── probability calibration ─────────────────────────────────────────────────
MIN_CALIBRATION_TRAIN = 300
MIN_CALIBRATION_TEST = 100


def _brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys)


def _logloss(ps, ys):
    eps = 1e-6
    return -sum(y * math.log(min(1 - eps, max(eps, p))) + (1 - y) * math.log(min(1 - eps, max(eps, 1 - p)))
                for p, y in zip(ps, ys)) / len(ys)


def _reliability(ps, ys, bins: int = 10):
    buckets = defaultdict(list)
    for p, y in zip(ps, ys):
        buckets[min(bins - 1, int(p * bins))].append((p, y))
    table, ece = [], 0.0
    n = len(ys)
    for b in sorted(buckets):
        items = buckets[b]
        mp = sum(p for p, _ in items) / len(items)
        oy = sum(y for _, y in items) / len(items)
        ece += len(items) / n * abs(mp - oy)
        table.append({"bucket": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "n": len(items),
                      "mean_predicted": mp, "observed_rate": oy})
    return table, ece


def _fit_platt(xs, ys, iters: int = 50):
    a = b = 0.0
    for _ in range(iters):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            p = 1 / (1 + math.exp(-(a * x + b)))
            w = p * (1 - p)
            g0 += (p - y) * x
            g1 += (p - y)
            h00 += w * x * x
            h01 += w * x
            h11 += w
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            break
        a -= (h11 * g0 - h01 * g1) / det
        b -= (-h01 * g0 + h00 * g1) / det
    return a, b


def calibration_report(rows: Sequence[dict], heuristic: Callable[[float], float]) -> dict:
    """Heuristic p vs outcomes with purged TRAIN / CALIBRATION / FINAL TEST.

    Outcome y = 1 when net R > 0. Platt scaling is fitted on CALIBRATION only
    and evaluated once on FINAL TEST against a base-rate predictor (rate from
    TRAIN+CALIBRATION). ``promotion_eligible`` requires adequate samples,
    Brier AND log-loss improvement over the base rate, ECE <= 0.05, and a
    positive 24h-block-bootstrap lower bound of the Brier improvement
    (stability across blocks). Otherwise the heuristic stays telemetry only.
    """
    from bot import nexus_oos_inference as inf

    usable = [r for r in rows if r.get("nexus_confidence") is not None and r.get("approved")
              and r.get("r") is not None and inf._is_resolved(r)]
    split = inf.purged_split(usable, (0.4, 0.3, 0.3))
    train, calib, test = split["train"], split["validation"], split["test"]
    out: dict = {"approved_rows": len(usable), "train": len(train), "calibration": len(calib),
                 "test": len(test), "split": inf.split_summary(split),
                 "outcome_definition": "net_r > 0",
                 "method": "PLATT_ON_CALIBRATION_EVAL_ON_FINAL_TEST", "promotion_eligible": False}
    if not test:
        out["status"] = "INSUFFICIENT_EVIDENCE"
        out["calibrated"] = None
        return out
    ys = [1 if float(r["r"]) > 0 else 0 for r in test]
    heur = [heuristic(float(r["nexus_confidence"])) for r in test]
    ref = train + calib
    base_rate = (sum(1 for r in ref if float(r["r"]) > 0) / len(ref)) if ref else 0.5
    rel, ece = _reliability(heur, ys)
    out["heuristic_on_test"] = {"brier": _brier(heur, ys), "log_loss": _logloss(heur, ys),
                                "ece": ece, "reliability": rel}
    base = [base_rate] * len(ys)
    out["base_rate_on_test"] = {"base_rate": base_rate, "brier": _brier(base, ys),
                                "log_loss": _logloss(base, ys)}
    if len(calib) < MIN_CALIBRATION_TRAIN or len(test) < MIN_CALIBRATION_TEST:
        out["status"] = "INSUFFICIENT_EVIDENCE"
        out["calibrated"] = None
        return out
    xs_c = [float(r["nexus_confidence"]) / 100.0 for r in calib]
    ys_c = [1 if float(r["r"]) > 0 else 0 for r in calib]
    a_, b_ = _fit_platt(xs_c, ys_c)

    def _p(r):
        return 1 / (1 + math.exp(-(a_ * float(r["nexus_confidence"]) / 100.0 + b_)))
    cal = [_p(r) for r in test]
    rel_c, ece_c = _reliability(cal, ys)
    out["calibrated"] = {"platt_a": a_, "platt_b": b_, "brier": _brier(cal, ys),
                         "log_loss": _logloss(cal, ys), "ece": ece_c, "reliability": rel_c}

    def _improvement(draw):
        if not draw:
            return None
        y = [1 if float(r["r"]) > 0 else 0 for r in draw]
        return _brier([base_rate] * len(y), y) - _brier([_p(r) for r in draw], y)
    blk = inf.required_block_ms(test)
    lo, hi = inf.block_bootstrap_ci(test, _improvement, samples=500, block_ms=blk)
    if len({inf.block_id(r["ts"], blk) for r in test}) < inf.MIN_RESAMPLING_BLOCKS:
        lo = hi = None     # insufficient independent blocks: no authority
    out["brier_improvement_block_ci"] = [lo, hi]
    out["brier_improvement_block_days"] = blk / inf.DAY_MS
    beats_brier = out["calibrated"]["brier"] < out["base_rate_on_test"]["brier"]
    beats_ll = out["calibrated"]["log_loss"] < out["base_rate_on_test"]["log_loss"]
    eligible = bool(beats_brier and beats_ll and ece_c <= 0.05 and lo is not None and lo > 0)
    out["promotion_eligible"] = eligible
    out["status"] = ("CALIBRATION_PROMOTION_ELIGIBLE" if eligible else
                     "CALIBRATION_NOT_PROMOTABLE_HEURISTIC_REMAINS_TELEMETRY")
    return out


def summarize_rows(rows: Iterable[dict], runtime_threshold: float) -> dict:
    """Convenience bundle used by the replay artifact."""
    rows = list(rows)
    base = rows
    approved = [r for r in rows if r.get("approved")]
    return {
        "baseline": performance(base),
        "approved": performance(approved),
        "rejection_rate": (1 - len(approved) / len(base)) if base else None,
        "runtime_threshold": runtime_threshold,
    }
