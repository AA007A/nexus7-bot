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
  AND the selected rows span >= MIN_INDEPENDENT_BLOCKS distinct blocks;
* authority = min lower / max upper across every authoritative predeclared
  length (PREDECLARED_BLOCK_DAYS plus the required length itself);
* no authoritative length => authority is None with an explicit status
  (AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON / INSUFFICIENT_INDEPENDENT_BLOCKS);
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
# >= MIN_INDEPENDENT_BLOCKS blocks. Never the most favourable one.
PREDECLARED_BLOCK_DAYS = (1, 2, 3, 7, 14, 30)
AUTHORITY_BLOCKS_MS = (DEFAULT_BLOCK_MS, SENSITIVITY_BLOCK_MS, LONG_BLOCK_MS)  # display only
# Percentile cluster bootstraps under-cover badly with few clusters; 30 is the
# conventional minimum number of independent clusters. Predeclared, not tuned.
MIN_INDEPENDENT_BLOCKS = 30
AUTHORITY_MODEL = "HORIZON_AWARE_BLOCK_BOOTSTRAP_V2"
IID_ROLE = "DIAGNOSTIC_ONLY"
AUTHORITY_VALID = "VALID"
AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON = "AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON"
INSUFFICIENT_INDEPENDENT_BLOCKS = "INSUFFICIENT_INDEPENDENT_BLOCKS"
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
                  "horizon, whole UTC days) AND >= %d independent blocks; authority = min lower / "
                  "max upper over every such predeclared block length; IID diagnostic only"
                  % MIN_INDEPENDENT_BLOCKS)


def _pack(rows, selector_for_blocks, iid, cis: dict, req_ms: int, min_blocks: int) -> dict:
    intervals = []
    for ms, ci in sorted(cis.items()):
        n_blk = _n_blocks(rows, selector_for_blocks, ms)
        if ms < req_ms:
            reason = AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON
        elif n_blk < min_blocks:
            reason = INSUFFICIENT_INDEPENDENT_BLOCKS
        elif ci[0] is None or ci[1] is None:
            reason = "CI_NOT_ESTIMABLE"
        else:
            reason = None
        intervals.append({"block_days": ms / DAY_MS, "block_ms": int(ms), "ci": list(ci),
                          "independent_blocks": n_blk, "authoritative": reason is None,
                          "invalid_reason": reason})
    valid = [iv for iv in intervals if iv["authoritative"]]
    auth = conservative_interval(*[tuple(iv["ci"]) for iv in valid])
    if valid:
        status = AUTHORITY_VALID
    elif any(iv["invalid_reason"] == INSUFFICIENT_INDEPENDENT_BLOCKS for iv in intervals
             if iv["block_ms"] >= req_ms):
        status = INSUFFICIENT_INDEPENDENT_BLOCKS
    elif all(iv["block_ms"] < req_ms for iv in intervals):
        status = AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON
    else:
        status = "CI_NOT_ESTIMABLE"
    out = {"iid_ci": list(iid), "iid_role": IID_ROLE, "block_intervals": intervals,
           "required_block_ms": int(req_ms), "required_block_days": req_ms / DAY_MS,
           "min_independent_blocks": int(min_blocks)}
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
                          min_blocks: int = MIN_INDEPENDENT_BLOCKS) -> dict:
    """IID (diagnostic) and block CIs of the mean R of selected RESOLVED rows.

    Censored rows are ignored. Authority exists only for block lengths >= the
    required dependence horizon with enough independent blocks; otherwise
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
                          min_blocks: int = MIN_INDEPENDENT_BLOCKS) -> dict:
    rows = [r for r in rows if _is_resolved(r) and r.get("r") is not None]
    req = int(required_ms) if required_ms is not None else required_block_ms(rows)
    stat = diff_mean_r(sel_a, sel_b)
    iid = fast_ci(rows, sel_a, sel_b, block_ms=None, samples=samples)
    cis = {ms: fast_ci(rows, sel_a, sel_b, block_ms=ms, samples=samples) for ms in _lengths(req)}
    out = {"delta": stat(rows)}
    out.update(_pack(rows, sel_a, iid, cis, req, min_blocks))
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
        "min_independent_blocks": MIN_INDEPENDENT_BLOCKS,
        "note": ("Block lengths are fixed in advance. Only lengths >= the required "
                 "dependence horizon with enough independent blocks carry authority."),
    }


# ── effective sample size ────────────────────────────────────────────────────
def effective_sample(rows: Sequence[dict], selector: Callable[[dict], bool] = lambda r: True,
                     block_ms: int = DEFAULT_BLOCK_MS) -> dict:
    """Raw counts plus a design-effect effective sample size.

    n_eff = n / (1 + (m_bar - 1) * icc), with the one-way ANOVA intra-block
    correlation of R clipped to [0, 1]. ``unique_blocks`` is also reported
    and is the more conservative independent-unit count.
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
