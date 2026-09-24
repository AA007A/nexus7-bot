"""Dependence-aware inference for NEXUS OOS research (pure, no I/O).

Candidate rows are NOT independent observations:

* outcomes overlap in time. The production-parity replay has NO time exit, so
  the overlap horizon is data-driven: the maximum RESOLVED outcome horizon
  (decision to final exit) of the rows being analysed. Right-censored rows are
  never completed trades and are excluded from every statistic here;
* crypto assets move together during market-wide events.

This module resamples UTC time BLOCKS (clustered bootstrap). Every candidate
of every symbol whose decision falls in a selected block is carried together.

Authority (fail closed):
* a block length is authoritative only if it is >= the required dependence
  horizon (ceil of the maximum resolved outcome horizon, in whole UTC days)
  AND the selected rows span >= MIN_RESAMPLING_BLOCKS distinct blocks;
* authority = min lower / max upper across every authoritative predeclared
  length (PREDECLARED_BLOCK_DAYS plus the required length itself);
* no authoritative length => authority is None with an explicit status
  (AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON / INSUFFICIENT_RESAMPLING_BLOCKS);
* the IID row bootstrap is DIAGNOSTIC ONLY and can never substitute.

Block lengths are fixed in advance and never chosen by outcome.

Splits are temporal and purged: a candidate whose outcome_end_ts reaches the
next partition is removed (censored rows are open-ended and always purged from
non-final partitions), and an embargo >= the longest resolved outcome horizon
separates partitions.
"""
from __future__ import annotations

from collections import defaultdict
import math
import random
from typing import Callable, Iterable, Sequence

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
# The legacy replay closed every trade within 40 x 15m = 10 h. Production has no
# time exit, so this is NOT a dependence horizon for the production-parity
# replay; it is kept only for the legacy diagnostic model.
LEGACY_REPLAY_MAX_HORIZON_MS = 40 * 15 * 60 * 1000
DEFAULT_BLOCK_MS = DAY_MS
SENSITIVITY_BLOCK_MS = 2 * DAY_MS
LONG_BLOCK_MS = 3 * DAY_MS
# Predeclared block lengths (days). Authority candidates are those whose length
# is >= the required dependence horizon (ceil of the MAXIMUM resolved outcome
# horizon, in whole UTC days) plus that required length itself. Authority is
# the most conservative interval across every candidate that is also backed by
# >= MIN_RESAMPLING_BLOCKS blocks. Never the most favourable one.
PREDECLARED_BLOCK_DAYS = (1, 2, 3, 7, 14, 30)
AUTHORITY_BLOCKS_MS = (DEFAULT_BLOCK_MS, SENSITIVITY_BLOCK_MS, LONG_BLOCK_MS)  # display only
# Percentile cluster bootstraps under-cover badly with few clusters; 30 is the
# conventional minimum number of resampling clusters. Predeclared, not tuned.
# Non-overlapping blocks keep within-block dependence; they do NOT prove that
# adjacent blocks are statistically independent (see the residual-dependence
# rule below).
MIN_RESAMPLING_BLOCKS = 30
AUTHORITY_MODEL = "HORIZON_AWARE_BLOCK_BOOTSTRAP_V2"
IID_ROLE = "DIAGNOSTIC_ONLY"
AUTHORITY_VALID = "VALID"
AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON = "AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON"
INSUFFICIENT_RESAMPLING_BLOCKS = "INSUFFICIENT_RESAMPLING_BLOCKS"
RESIDUAL_DEPENDENCE = "RESIDUAL_DEPENDENCE_AT_LONGEST_USABLE_BLOCK"
# Predeclared residual-dependence rule (fixed before results): at the LONGEST
# block length that is otherwise usable for authority, compute the ACF of
# consecutive block-mean R at lags 1..3. If any |ACF| exceeds the 2/sqrt(n)
# reference band, dependence spills across blocks even at the most
# conservative usable length: no authority (INSUFFICIENT_EVIDENCE). A shorter
# block is never substituted.
RESIDUAL_ACF_LAGS = (1, 2, 3)
# Lags are CALENDAR lags between block ids (a missing block breaks adjacency).
# Each lag needs >= this many calendar-valid pairs, otherwise the rule cannot
# be evaluated and authority is withheld (INSUFFICIENT_EVIDENCE). Predeclared.
MIN_RESIDUAL_ACF_PAIRS = 20
RESIDUAL_NOT_ESTIMABLE = "RESIDUAL_DEPENDENCE_NOT_ESTIMABLE"
# Difference statistics (mean(A) - mean(B), e.g. uplift) use their OWN residual
# series: the per-block paired delta mean_A(block) - mean_B(block), defined only
# for blocks containing both populations. Never the A-only block means.
SERIES_KIND_MEAN = "BLOCK_MEAN"
SERIES_KIND_PAIRED = "PAIRED_BLOCK_DELTA"
PAIRED_RESIDUAL_DEPENDENCE = "UPLIFT_RESIDUAL_DEPENDENCE"
PAIRED_RESIDUAL_NOT_ESTIMABLE = "UPLIFT_RESIDUAL_DEPENDENCE_NOT_ESTIMABLE"
BOOTSTRAP_SAMPLES = 2000
SEED = 11


def block_id(ts_ms: int, block_ms: int = DEFAULT_BLOCK_MS) -> int:
    return int(ts_ms) // int(block_ms)


def _blocks(rows: Sequence[dict], block_ms: int) -> list[list[dict]]:
    groups: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        groups[block_id(r["ts"], block_ms)].append(r)
    return [groups[k] for k in sorted(groups)]


def _percentile_ci(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 100:
        return None, None
    values.sort()
    return values[int(0.025 * len(values))], values[min(len(values) - 1, int(0.975 * len(values)))]


def iid_bootstrap_ci(rows: Sequence[dict], stat: Callable[[list[dict]], float | None], *,
                     samples: int = BOOTSTRAP_SAMPLES, seed: int = SEED):
    """Diagnostic only: treats rows as independent (they are not)."""
    n = len(rows)
    if n < 2:
        return None, None
    rng = random.Random(seed)
    vals = []
    for _ in range(samples):
        v = stat([rows[rng.randrange(n)] for _ in range(n)])
        if v is not None:
            vals.append(v)
    return _percentile_ci(vals)


def block_bootstrap_ci(rows: Sequence[dict], stat: Callable[[list[dict]], float | None], *,
                       block_ms: int = DEFAULT_BLOCK_MS, samples: int = BOOTSTRAP_SAMPLES,
                       seed: int = SEED):
    """Clustered bootstrap over UTC blocks (all symbols in a block stay together)."""
    blocks = _blocks(rows, block_ms)
    k = len(blocks)
    if k < 2:
        return None, None
    rng = random.Random(seed)
    vals = []
    for _ in range(samples):
        draw: list[dict] = []
        for _ in range(k):
            draw.extend(blocks[rng.randrange(k)])
        v = stat(draw)
        if v is not None:
            vals.append(v)
    return _percentile_ci(vals)


def mean_r(selector: Callable[[dict], bool] = lambda r: True) -> Callable[[list[dict]], float | None]:
    def _stat(draw):
        xs = [float(r["r"]) for r in draw if selector(r)]
        return sum(xs) / len(xs) if xs else None
    return _stat


def diff_mean_r(sel_a: Callable[[dict], bool], sel_b: Callable[[dict], bool]):
    def _stat(draw):
        a = [float(r["r"]) for r in draw if sel_a(r)]
        b = [float(r["r"]) for r in draw if sel_b(r)]
        if not a or not b:
            return None
        return sum(a) / len(a) - sum(b) / len(b)
    return _stat


def _unit_arrays(rows, key, sel_a, sel_b=None):
    """Per-unit (block or row) sums/counts for selected rows."""
    import numpy as np
    units: dict = defaultdict(lambda: [0.0, 0, 0.0, 0])
    for i, r in enumerate(rows):
        u = units[key(i, r)]
        x = float(r["r"])
        if sel_a(r):
            u[0] += x
            u[1] += 1
        if sel_b is not None and sel_b(r):
            u[2] += x
            u[3] += 1
    arr = np.array(list(units.values()), dtype=float) if units else np.zeros((0, 4))
    return arr


def _fast_ci(arr, *, diff: bool, samples: int, seed: int, chunk: int = 250):
    import numpy as np
    k = arr.shape[0]
    if k < 2:
        return None, None
    rng = np.random.default_rng(seed)
    vals = []
    done = 0
    while done < samples:
        m = min(chunk, samples - done)
        idx = rng.integers(0, k, size=(m, k))
        sa, na = arr[idx, 0].sum(1), arr[idx, 1].sum(1)
        with np.errstate(invalid="ignore", divide="ignore"):
            va = sa / na
            if diff:
                sb, nb = arr[idx, 2].sum(1), arr[idx, 3].sum(1)
                va = va - sb / nb
        vals.extend(float(v) for v in va if np.isfinite(v))
        done += m
    return _percentile_ci(vals)


def fast_ci(rows, sel_a, sel_b=None, *, block_ms: int | None, samples: int = BOOTSTRAP_SAMPLES,
            seed: int = SEED):
    """Vectorized bootstrap. block_ms=None resamples rows IID (diagnostic)."""
    rows = list(rows)
    if block_ms is None:
        key = lambda i, r: i  # noqa: E731
    else:
        key = lambda i, r: block_id(r["ts"], block_ms)  # noqa: E731
    return _fast_ci(_unit_arrays(rows, key, sel_a, sel_b), diff=sel_b is not None,
                    samples=samples, seed=seed)


def lag1_block_autocorrelation(rows: Sequence[dict], block_ms: int = DEFAULT_BLOCK_MS) -> dict:
    """Lag-1 autocorrelation of consecutive block mean R (dependence diagnostic)."""
    means = [sum(float(r["r"]) for r in b) / len(b) for b in _blocks(rows, block_ms) if b]
    n = len(means)
    if n < 10:
        return {"blocks": n, "acf1": None, "significance_band": None}
    mu = sum(means) / n
    den = sum((m - mu) ** 2 for m in means)
    num = sum((means[i] - mu) * (means[i - 1] - mu) for i in range(1, n))
    acf = num / den if den > 0 else None
    band = 2 / math.sqrt(n)
    return {"blocks": n, "acf1": acf, "significance_band": band,
            "acf1_significant": (abs(acf) > band) if acf is not None else None}


def _is_resolved(r: dict) -> bool:
    status = r.get("outcome_status")
    return not r.get("censored") and (status is None or status == "RESOLVED")


def outcome_horizon_stats(rows: Sequence[dict]) -> dict:
    """Distribution of RESOLVED outcome horizons (decision -> final exit)."""
    hs = sorted(int(r.get("outcome_end_ts", r["ts"])) - int(r["ts"])
                for r in rows if _is_resolved(r))
    if not hs:
        return {"n_resolved": 0, "median_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}

    def q(p):
        return hs[min(len(hs) - 1, int(p * (len(hs) - 1) + 0.5))]
    return {"n_resolved": len(hs), "median_ms": q(0.5), "p95_ms": q(0.95), "p99_ms": q(0.99),
            "max_ms": hs[-1],
            "median_hours": q(0.5) / HOUR_MS, "p95_hours": q(0.95) / HOUR_MS,
            "p99_hours": q(0.99) / HOUR_MS, "max_hours": hs[-1] / HOUR_MS}


def required_block_ms(rows: Sequence[dict]) -> int:
    """Smallest whole number of UTC days >= the MAXIMUM resolved outcome horizon
    (minimum 1 day). With blocks at least this long an outcome can overlap only
    the adjacent block (m-dependence at block level)."""
    mx = outcome_horizon_stats(rows)["max_ms"] or 0
    return max(1, math.ceil(mx / DAY_MS)) * DAY_MS


def _n_blocks(rows, selector, block_ms) -> int:
    return len({block_id(r["ts"], block_ms) for r in rows if selector(r)})


def _authority_candidates(req_ms: int) -> list[int]:
    lengths = {d * DAY_MS for d in PREDECLARED_BLOCK_DAYS if d * DAY_MS >= req_ms}
    lengths.add(int(req_ms))
    return sorted(lengths)


def conservative_interval(*intervals: tuple[float | None, float | None]) -> dict:
    """Min lower / max upper over the VALID intervals passed in."""
    valid = [(lo, hi) for lo, hi in intervals if lo is not None and hi is not None]
    if not valid:
        return {"low": None, "high": None, "valid_intervals": 0}
    return {"low": min(lo for lo, _ in valid), "high": max(hi for _, hi in valid),
            "valid_intervals": len(valid)}


def block_authority(block_intervals: dict) -> dict:
    """Authority from BLOCK intervals only. IID is never an argument here."""
    return conservative_interval(*block_intervals.values())


def _block_key(block_ms: int) -> str:
    return f"block{int(block_ms) // HOUR_MS}h_ci"


AUTHORITY_RULE = ("block length >= required dependence horizon (ceil max resolved outcome "
                  "horizon, whole UTC days) AND >= %d resampling blocks AND no significant residual block dependence (lags 1-3) at the longest usable length; authority = min lower / "
                  "max upper over every such predeclared block length; IID diagnostic only"
                  % MIN_RESAMPLING_BLOCKS)


def block_acf(rows, selector, block_ms: int, lags=RESIDUAL_ACF_LAGS) -> dict:
    """CALENDAR-correct residual ACF of block-mean R (selected rows).

    Emits the compact series (block ids, means, counts; no individual trades)
    so the promotion gate can recompute everything independently.
    """
    groups: dict = defaultdict(list)
    for r in rows:
        if selector(r):
            groups[block_id(r["ts"], block_ms)].append(float(r["r"]))
    ordered = sorted(groups.items())
    series = {"block_ids": [int(b) for b, _ in ordered],
              "means": [round(sum(v) / len(v), 12) for _, v in ordered],
              "counts": [len(v) for _, v in ordered]}
    res = acf_from_block_series(series["block_ids"], series["means"], lags)
    return {"residual_series_kind": SERIES_KIND_MEAN, "n_blocks": len(ordered), "series": series, **res}


def paired_delta_from_aggregates(series) -> tuple[list[int], list[float]]:
    """Predeclared block-level delta: mean_A - mean_B = a_sum/a_count - b_sum/b_count,
    only for blocks where BOTH counts are >= 1 (others are dropped and so break
    calendar adjacency). Shared by inference and the promotion gate."""
    ids, deltas = [], []
    for b, sa, na, sb, nb in zip(series["block_ids"], series["a_sum"], series["a_count"],
                                 series["b_sum"], series["b_count"]):
        if na >= 1 and nb >= 1:
            ids.append(int(b))
            deltas.append(float(sa) / na - float(sb) / nb)
    return ids, deltas


def paired_block_acf(rows, sel_a, sel_b, block_ms: int, *, min_blocks: int = MIN_RESAMPLING_BLOCKS,
                     lags=RESIDUAL_ACF_LAGS) -> dict:
    """Residual dependence of a DIFFERENCE statistic (mean_A - mean_B).

    Per calendar block the artifact carries a_sum, a_count, b_sum, b_count
    (both selections jointly, no individual trades). The residual series is
    the paired block delta; fewer than ``min_blocks`` estimable deltas or too
    few calendar pairs => not estimable (fail closed).
    """
    agg: dict = defaultdict(lambda: [0.0, 0, 0.0, 0])
    for r in rows:
        in_a, in_b = sel_a(r), sel_b(r)
        if not (in_a or in_b):
            continue
        g = agg[block_id(r["ts"], block_ms)]
        if in_a:
            g[0] += float(r["r"])
            g[1] += 1
        if in_b:
            g[2] += float(r["r"])
            g[3] += 1
    ordered = sorted(agg.items())
    series = {"block_ids": [int(b) for b, _ in ordered],
              "a_sum": [round(v[0], 12) for _, v in ordered], "a_count": [v[1] for _, v in ordered],
              "b_sum": [round(v[2], 12) for _, v in ordered], "b_count": [v[3] for _, v in ordered]}
    ids, deltas = paired_delta_from_aggregates(series)
    res = acf_from_block_series(ids, deltas, lags)
    if len(deltas) < min_blocks:
        res = {**res, "estimable": False, "significant": None,
               "not_estimable_reason": "TOO_FEW_BLOCKS_WITH_BOTH_POPULATIONS"}
    return {"residual_series_kind": SERIES_KIND_PAIRED, "delta_definition":
            "a_sum/a_count - b_sum/b_count per block; blocks lacking A or B are dropped",
            "n_blocks": len(ordered), "n_delta_blocks": len(deltas),
            "min_delta_blocks": int(min_blocks), "series": series, **res}


def validate_block_series(series, expected_blocks=None) -> str | None:
    """Structural checks on a compact block series. None when valid."""
    if not isinstance(series, dict):
        return "RESIDUAL_DEPENDENCE_SERIES_MISSING"
    ids, means, counts = series.get("block_ids"), series.get("means"), series.get("counts")
    if not all(isinstance(x, list) for x in (ids, means, counts)):
        return "RESIDUAL_DEPENDENCE_SERIES_MISSING"
    if not (len(ids) == len(means) == len(counts)):
        return "RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT"
    if any(isinstance(b, bool) or not isinstance(b, int) for b in ids):
        return "RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT"
    if any(b2 <= b1 for b1, b2 in zip(ids, ids[1:])):          # strictly increasing => unique
        return "RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT"
    if any(isinstance(c, bool) or not isinstance(c, int) or c < 1 for c in counts):
        return "RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT"
    for m in means:
        if isinstance(m, bool) or not isinstance(m, (int, float)) or not math.isfinite(m):
            return "RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT"
    if expected_blocks is not None and len(ids) != expected_blocks:
        return "RESIDUAL_DEPENDENCE_SERIES_INCONSISTENT"
    return None


def acf_from_block_series(block_ids, means, lags=RESIDUAL_ACF_LAGS,
                          min_pairs: int = MIN_RESIDUAL_ACF_PAIRS) -> dict:
    """Lag-k ACF using CALENDAR adjacency: pair block values only when
    ``block_id_j - block_id_i == k``. Missing blocks break adjacency.

    r_k = [mean over the N_k eligible pairs of (x_i - mu)(x_j - mu)] / var,
    reference band 2/sqrt(N_k). NOT ESTIMABLE (fail closed) when any lag has
    fewer than ``min_pairs`` pairs, or when the series has zero variance (the
    ACF is undefined; a constant series is not proof of independence).
    """
    n = len(means)
    per = {}
    by_id = dict(zip(block_ids, means))
    mu = (sum(means) / n) if n else 0.0
    var = (sum((m - mu) ** 2 for m in means) / n) if n else 0.0
    reason = None
    # Zero variance (up to floating-point residue): the ACF is undefined.
    spread = (max(means) - min(means)) if n else 0.0
    if var <= 0 or spread <= 1e-12 * max(1.0, abs(mu)):
        var = 0.0
        reason = "ZERO_VARIANCE"
    for k in lags:
        pairs = [(by_id[b], by_id[b + k]) for b in block_ids if b + k in by_id]
        npairs = len(pairs)
        if npairs < min_pairs or var <= 0:
            per[str(k)] = {"eligible_pairs": npairs, "acf": None, "reference_band": None,
                           "significant": None}
            if npairs < min_pairs:
                reason = reason or "INSUFFICIENT_CALENDAR_PAIRS"
            continue
        acf = (sum((a - mu) * (b - mu) for a, b in pairs) / npairs) / var
        band = 2 / math.sqrt(npairs)
        per[str(k)] = {"eligible_pairs": npairs, "acf": acf, "reference_band": band,
                       "significant": abs(acf) > band}
    estimable = reason is None
    return {"lags": per, "acf": {k: v["acf"] for k, v in per.items()},
            "min_calendar_pairs": int(min_pairs), "estimable": estimable,
            "not_estimable_reason": reason,
            "significant": (any(v["significant"] for v in per.values()) if estimable else None)}


def _pack(rows, selector_for_blocks, iid, cis: dict, req_ms: int, min_blocks: int,
          residual_fn=None, kind: str = SERIES_KIND_MEAN) -> dict:
    paired = kind == SERIES_KIND_PAIRED
    dep_code = PAIRED_RESIDUAL_DEPENDENCE if paired else RESIDUAL_DEPENDENCE
    nest_code = PAIRED_RESIDUAL_NOT_ESTIMABLE if paired else RESIDUAL_NOT_ESTIMABLE
    intervals = []
    for ms, ci in sorted(cis.items()):
        n_blk = _n_blocks(rows, selector_for_blocks, ms)
        if ms < req_ms:
            reason = AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON
        elif n_blk < min_blocks:
            reason = INSUFFICIENT_RESAMPLING_BLOCKS
        elif ci[0] is None or ci[1] is None:
            reason = "CI_NOT_ESTIMABLE"
        else:
            reason = None
        intervals.append({"block_days": ms / DAY_MS, "block_ms": int(ms), "ci": list(ci),
                          "resampling_blocks": n_blk, "authoritative": reason is None,
                          "invalid_reason": reason,
                          "residual_dependence": ((residual_fn(ms) if residual_fn is not None
                                                   else block_acf(rows, selector_for_blocks, ms))
                                                  if ms >= req_ms else None)})
    usable = [iv for iv in intervals if iv["authoritative"]]
    residual = None
    if usable:
        longest = max(usable, key=lambda iv: iv["block_ms"])
        residual = {"longest_usable_block_days": longest["block_days"],
                    **(longest["residual_dependence"] or {})}
        if residual.get("significant") or residual.get("estimable") is not True:
            reason = (dep_code if residual.get("significant") else nest_code)
            for iv in usable:
                iv["authoritative"] = False
                iv["invalid_reason"] = reason
    valid = [iv for iv in intervals if iv["authoritative"]]
    auth = conservative_interval(*[tuple(iv["ci"]) for iv in valid])
    if valid:
        status = AUTHORITY_VALID
    elif residual is not None and residual.get("significant"):
        status = dep_code
    elif residual is not None:
        status = nest_code
    elif any(iv["invalid_reason"] == INSUFFICIENT_RESAMPLING_BLOCKS for iv in intervals
             if iv["block_ms"] >= req_ms):
        status = INSUFFICIENT_RESAMPLING_BLOCKS
    elif all(iv["block_ms"] < req_ms for iv in intervals):
        status = AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON
    else:
        status = "CI_NOT_ESTIMABLE"
    out = {"iid_ci": list(iid), "iid_role": IID_ROLE, "block_intervals": intervals,
           "required_block_ms": int(req_ms), "required_block_days": req_ms / DAY_MS,
           "min_resampling_blocks": int(min_blocks),
           "residual_series_kind": kind,
           "residual_dependence_rule": ("CALENDAR lags 1-3 of the %s series (pairs only when block ids " % kind +
                                        "differ by exactly k); any |ACF| above 2/sqrt(pairs), or fewer "
                                        "than %d pairs at any lag, or zero variance, at the " % MIN_RESIDUAL_ACF_PAIRS +
                                        "longest usable block length => no authority"),
           "residual_dependence_at_longest_usable": residual}
    for ms in AUTHORITY_BLOCKS_MS:          # legacy display keys (diagnostic)
        if ms in cis:
            out[_block_key(ms)] = list(cis[ms])
    out.update({
        "authority_ci_low": auth["low"],
        "authority_ci_high": auth["high"],
        "authority_valid_block_intervals": auth["valid_intervals"],
        "authority_status": status,
        "authority_rule": AUTHORITY_RULE,
        "authority_model": AUTHORITY_MODEL,
    })
    return out


def _lengths(req_ms: int) -> list[int]:
    return sorted(set(AUTHORITY_BLOCKS_MS) | set(_authority_candidates(req_ms)))


def dependence_aware_mean(rows: Sequence[dict], selector: Callable[[dict], bool] = lambda r: True,
                          *, samples: int = BOOTSTRAP_SAMPLES, required_ms: int | None = None,
                          min_blocks: int = MIN_RESAMPLING_BLOCKS) -> dict:
    """IID (diagnostic) and block CIs of the mean R of selected RESOLVED rows.

    Censored rows are ignored. Authority exists only for block lengths >= the
    required dependence horizon with enough resampling blocks; otherwise
    ``authority_ci_low/high`` are None (fail closed).
    """
    rows = [r for r in rows if _is_resolved(r) and r.get("r") is not None]
    req = int(required_ms) if required_ms is not None else required_block_ms(rows)
    iid = fast_ci(rows, selector, block_ms=None, samples=samples)
    cis = {ms: fast_ci(rows, selector, block_ms=ms, samples=samples) for ms in _lengths(req)}
    sel = [r for r in rows if selector(r)]
    out = {"mean_r": (sum(float(r["r"]) for r in sel) / len(sel)) if sel else None, "n": len(sel)}
    out.update(_pack(rows, selector, iid, cis, req, min_blocks))
    return out


def dependence_aware_diff(rows: Sequence[dict], sel_a, sel_b, *,
                          samples: int = BOOTSTRAP_SAMPLES, required_ms: int | None = None,
                          min_blocks: int = MIN_RESAMPLING_BLOCKS) -> dict:
    rows = [r for r in rows if _is_resolved(r) and r.get("r") is not None]
    req = int(required_ms) if required_ms is not None else required_block_ms(rows)
    stat = diff_mean_r(sel_a, sel_b)
    iid = fast_ci(rows, sel_a, sel_b, block_ms=None, samples=samples)
    cis = {ms: fast_ci(rows, sel_a, sel_b, block_ms=ms, samples=samples) for ms in _lengths(req)}
    out = {"delta": stat(rows)}
    out.update(_pack(rows, sel_a, iid, cis, req, min_blocks,
                     residual_fn=lambda ms: paired_block_acf(rows, sel_a, sel_b, ms, min_blocks=min_blocks),
                     kind=SERIES_KIND_PAIRED))
    return out


def dependence_diagnostics(rows: Sequence[dict], max_lag: int = 3) -> dict:
    """Autocorrelation of daily-block mean R at lags 1..max_lag plus the
    per-block-length lag-1 check. Supports (not selects) the block lengths."""
    rows = [r for r in rows if _is_resolved(r) and r.get("r") is not None]
    daily = [sum(float(r["r"]) for r in b) / len(b) for b in _blocks(rows, DAY_MS) if b]
    n = len(daily)
    acf = {}
    if n >= 10:
        mu = sum(daily) / n
        den = sum((m - mu) ** 2 for m in daily)
        for lag in range(1, max_lag + 1):
            num = sum((daily[i] - mu) * (daily[i - lag] - mu) for i in range(lag, n))
            acf[str(lag)] = (num / den) if den > 0 else None
    band = (2 / math.sqrt(n)) if n >= 10 else None
    significant_lags = [int(k) for k, v in acf.items() if v is not None and band and abs(v) > band]
    return {
        "daily_blocks": n,
        "daily_acf": acf,
        "significance_band": band,
        "significant_daily_lags": significant_lags,
        "per_block_length_lag1": {f"{d}d": lag1_block_autocorrelation(rows, d * DAY_MS)
                                  for d in PREDECLARED_BLOCK_DAYS},
        "outcome_horizon": outcome_horizon_stats(rows),
        "required_block_days": required_block_ms(rows) / DAY_MS,
        "block_lengths_predeclared_days": list(PREDECLARED_BLOCK_DAYS),
        "min_resampling_blocks": MIN_RESAMPLING_BLOCKS,
        "note": ("Block lengths are fixed in advance. Only lengths >= the required "
                 "dependence horizon with enough resampling blocks carry authority."),
    }


# ── effective sample size ────────────────────────────────────────────────────
def effective_sample(rows: Sequence[dict], selector: Callable[[dict], bool] = lambda r: True,
                     block_ms: int = DEFAULT_BLOCK_MS) -> dict:
    """Raw counts plus a design-effect effective sample size.

    n_eff = n / (1 + (m_bar - 1) * icc), with the one-way ANOVA intra-block
    correlation of R clipped to [0, 1]. ``unique_blocks`` is also reported
    and is the more conservative resampling-unit count.
    """
    sel = [r for r in rows if selector(r)]
    n = len(sel)
    groups = defaultdict(list)
    for r in sel:
        groups[block_id(r["ts"], block_ms)].append(float(r["r"]))
    k = len(groups)
    days = len({block_id(r["ts"], DAY_MS) for r in sel})
    out = {"rows": n, "unique_utc_days": days, "unique_blocks": k,
           "symbols": len({r.get("symbol") for r in sel}),
           "block_ms": block_ms, "icc": None, "design_effect": None, "effective_n": None}
    if n < 2 or k < 2:
        return out
    grand = sum(sum(v) for v in groups.values()) / n
    ssb = sum(len(v) * (sum(v) / len(v) - grand) ** 2 for v in groups.values())
    ssw = sum(sum((x - sum(v) / len(v)) ** 2 for x in v) for v in groups.values())
    msb = ssb / (k - 1)
    msw = ssw / (n - k) if n > k else 0.0
    m0 = (n - sum(len(v) ** 2 for v in groups.values()) / n) / (k - 1)
    icc = (msb - msw) / (msb + (m0 - 1) * msw) if (msb + (m0 - 1) * msw) > 0 else 0.0
    icc = max(0.0, min(1.0, icc))
    m_bar = n / k
    deff = 1 + (m_bar - 1) * icc
    out.update(icc=icc, design_effect=deff, effective_n=n / deff)
    return out


# ── purged / embargoed temporal splits ───────────────────────────────────────
def purged_split(rows: Sequence[dict], fractions: Sequence[float] = (0.5, 0.25, 0.25), *,
                 embargo_ms: int | None = None) -> dict:
    """TRAIN | purge+embargo | VALIDATION | purge+embargo | FINAL TEST.

    Boundaries come from the decision-timestamp span, not row order. A row
    belongs to a partition only if its decision_ts is inside it AND its
    outcome_end_ts ends before the partition's right boundary (purge). Each
    later partition starts ``embargo`` after the boundary. The embargo is at
    least the maximum observed outcome horizon and at least the replay's
    replay's resolved horizons.
    """
    rows = sorted(rows, key=lambda r: r["ts"])
    if not rows:
        return {"train": [], "validation": [], "test": [], "boundaries_ms": [], "embargo_ms": None,
                "purged": {}}
    # Embargo >= the longest RESOLVED outcome horizon (production has no time
    # exit, so no fixed 10 h floor). Censored rows have an unknown end: they
    # are treated as open-ended and purged from every non-final partition.
    horizons = [int(r.get("outcome_end_ts", r["ts"])) - int(r["ts"]) for r in rows if _is_resolved(r)]
    min_embargo = max(horizons) if horizons else 0
    embargo = max(int(embargo_ms or 0), min_embargo)
    t0, t1 = int(rows[0]["ts"]), int(rows[-1]["ts"])
    span = max(1, t1 - t0)
    cuts = []
    acc = 0.0
    for f in fractions[:-1]:
        acc += f
        cuts.append(t0 + int(span * acc))
    edges = [t0] + cuts + [t1 + 1]
    names = ("train", "validation", "test") if len(fractions) == 3 else tuple(f"part{i}" for i in range(len(fractions)))
    parts: dict = {n: [] for n in names}
    purged: dict = {n: 0 for n in names}
    for i, name in enumerate(names):
        left = edges[i] + (embargo if i > 0 else 0)
        right = edges[i + 1]
        last = i == len(names) - 1
        for r in rows:
            ts = int(r["ts"])
            if not (left <= ts < right):
                continue
            end = int(r.get("outcome_end_ts", ts)) if _is_resolved(r) else math.inf
            if not last and end >= right:
                purged[name] += 1
                continue
            parts[name].append(r)
    parts["boundaries_ms"] = cuts
    parts["embargo_ms"] = embargo
    parts["purged"] = purged
    return parts


def split_summary(split: dict) -> dict:
    out = {"boundaries_ms": split.get("boundaries_ms"), "embargo_ms": split.get("embargo_ms"),
           "embargo_hours": (split.get("embargo_ms") or 0) / HOUR_MS, "purged": split.get("purged")}
    for name in ("train", "validation", "test"):
        part = split.get(name) or []
        out[name] = {"rows": len(part),
                     "first_ts": part[0]["ts"] if part else None,
                     "last_ts": part[-1]["ts"] if part else None,
                     "max_outcome_end_ts": max((int(r.get("outcome_end_ts", r["ts"])) for r in part), default=None)}
    return out


def assert_no_leakage(split: dict) -> None:
    """Raise if any earlier-partition outcome reaches a later partition's window."""
    order = [n for n in ("train", "validation", "test") if split.get(n) is not None]
    for i, name in enumerate(order[:-1]):
        later = [r for n in order[i + 1:] for r in split[n]]
        if not later or not split[name]:
            continue
        first_later = min(int(r["ts"]) for r in later)
        last_end = max((int(r.get("outcome_end_ts", r["ts"])) if _is_resolved(r) else math.inf)
                       for r in split[name])
        if last_end >= first_later:
            raise AssertionError(f"{name} outcome overlaps a later partition")


def iter_selectors(names: Iterable[str]):
    for name in names:
        yield name, (lambda r, _n=name: bool((r.get("variants") or {}).get(_n)))


# ── purged / embargoed calendar folds (candidate and portfolio robustness) ──
# Predeclared: 4 folds; each fold's decision window must be >= MIN_FOLD_DECISION_DAYS.
FOLD_COUNT = 4
MIN_FOLD_DECISION_DAYS = 14
FOLDS_INSUFFICIENT = "INSUFFICIENT_INDEPENDENT_FOLDS"


def purged_calendar_folds(t0: int, t1: int, *, required_horizon_ms: int, embargo_ms: int | None = None,
                          folds: int = FOLD_COUNT,
                          min_decision_days: float = MIN_FOLD_DECISION_DAYS) -> dict:
    """Calendar-time fold layout over the decision span [t0, t1).

    fold k: DECISION_WINDOW [s_k, e_k) + OUTCOME_COMPLETION_WINDOW [e_k, e_k + H)
    + EMBARGO [e_k + H, e_k + H + E); the next fold's decisions start at
    s_{k+1} = e_k + H + E. H = ``required_horizon_ms`` (the authoritative
    resolved horizon) and E >= H always (a smaller embargo raises: fold count
    is never increased by shrinking the embargo). When the folds' decision
    windows would be shorter than ``min_decision_days`` the layout is
    INSUFFICIENT and no folds are produced.
    """
    req = int(required_horizon_ms)
    if embargo_ms is not None and int(embargo_ms) < req:
        raise ValueError("embargo shorter than the required outcome horizon")
    emb = max(req, int(embargo_ms or 0))
    gap = req + emb
    span = int(t1) - int(t0)
    width = (span - (folds - 1) * gap) // folds if folds > 0 else 0
    base = {"folds_requested": folds, "fold_embargo_ms": emb, "fold_required_horizon_ms": req,
            "fold_outcome_completion_ms": req, "fold_gap_ms": gap,
            "min_fold_decision_days": min_decision_days, "decision_span_days": span / DAY_MS,
            "fold_decision_window_days": max(0, width) / DAY_MS}
    if width < min_decision_days * DAY_MS:
        return {**base, "status": FOLDS_INSUFFICIENT, "windows": []}
    windows = []
    s = int(t0)
    for k in range(folds):
        e = s + width
        windows.append({"fold": k + 1, "decision_start_ts": s, "decision_end_ts": e,
                        "outcome_window_end_ts": e + req, "embargo_end_ts": e + gap})
        s = e + gap
    return {**base, "status": "OK", "windows": windows}


def assert_folds_overlap_free(folds: list[dict]) -> bool:
    """max(market time used by fold K) < min(decision_ts of fold K+1)."""
    for a, b in zip(folds, folds[1:]):
        used_end = a.get("max_market_ts_used")
        if used_end is None:
            used_end = int(a["outcome_window_end_ts"]) - 1   # window end is exclusive
        if int(used_end) >= int(b["decision_start_ts"]):
            raise AssertionError(f"fold {a['fold']} market time overlaps fold {b['fold']}")
    return True
