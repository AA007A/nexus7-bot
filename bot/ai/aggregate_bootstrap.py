"""AI_AGGREGATE_BLOCK_BOOTSTRAP_V1 — independent recomputation of the AI
authority intervals from compact per-block aggregates.

For every predeclared block length >= the required dependence horizon, the
artifact retains the per-block aggregates of the block universe the clustered
bootstrap resampled (``block_intervals[i].residual_dependence.series``):

  mean:   block_id, sum_r, count
  uplift: block_id, a_sum, a_count, b_sum, b_count

Each bootstrap draw is rebuilt from those aggregates:

  mean:   sum(sampled sum_r) / sum(sampled count)
  uplift: sum(sampled a_sum)/sum(sampled a_count) - sum(sampled b_sum)/sum(sampled b_count)

using the SAME predeclared seed (nexus_oos_inference.SEED), sample count,
block universe (ascending block id = the time order in which the replay
passed its rows), draw code (nexus_oos_inference._fast_ci) and 2.5/97.5
percentile definition. The point estimate, the influence-score residual ACF
and the authority interval (min low / max high over valid lengths) are all
recomputed. Stored ``block_intervals[].ci``, ``authority_ci_low/high``,
``authority_status``, stored ACF and stored point estimates are COMPARISON
fields only: tampering with them cannot change the recomputed verdict.
"""
from __future__ import annotations

import numpy as np

from bot import nexus_oos_inference as inf
from bot import nexus_oos_promotion_gate as g

VERSION = "AI_AGGREGATE_BLOCK_BOOTSTRAP_V1"
PERCENTILES = (0.025, 0.975)
CI_COMPARISON_TOLERANCE = 1e-6


def _arr(series: dict, *, diff: bool):
    if diff:
        cols = (series["a_sum"], series["a_count"], series["b_sum"], series["b_count"])
    else:
        n = len(series["block_ids"])
        cols = (series["sum_r"], series["count"], [0.0] * n, [0] * n)
    order = np.argsort(np.asarray(series["block_ids"], dtype=np.int64), kind="stable")
    return np.array(list(zip(*cols)), dtype=float)[order]


def ci_from_series(series: dict, *, diff: bool, samples: int, seed: int = inf.SEED):
    return inf._fast_ci(_arr(series, diff=diff), diff=diff, samples=int(samples), seed=int(seed))


def recompute(section: dict, *, required_ms: int | None, min_blocks: int, paired: bool,
              samples: int | None) -> dict:
    """Recomputed authority for one AI authority section (never trusts stored CIs)."""
    out = {"model": VERSION, "status": None, "ci": [None, None], "point_estimate": None,
           "intervals": [], "stored_comparison": {}}
    kind = inf.SERIES_KIND_DIFF if paired else inf.SERIES_KIND_MEAN
    if required_ms is None:
        out["status"] = "OUTCOME_HORIZON_UNKNOWN"
        return out
    if not isinstance(samples, int) or samples < 100:
        out["status"] = "BOOTSTRAP_SAMPLES_UNKNOWN"
        return out
    ivs = {int(iv["block_ms"]): iv for iv in (section or {}).get("block_intervals") or []
           if isinstance(iv, dict) and isinstance(iv.get("block_ms"), (int, float))}
    lengths = [ms for ms in inf._lengths(int(required_ms)) if ms >= int(required_ms)]
    if not lengths:
        out["status"] = "AUTHORITY_BLOCK_SHORTER_THAN_OUTCOME_HORIZON"
        return out
    usable, points = [], []
    for ms in lengths:
        iv = ivs.get(ms)
        rd = (iv or {}).get("residual_dependence")
        if not isinstance(rd, dict) or rd.get("residual_series_kind") != kind:
            out["status"] = "AGGREGATE_INTERVAL_MISSING"
            return out
        series = rd.get("series")
        if not isinstance(series, dict) or not isinstance(series.get("a_count" if paired else "count"), list):
            out["status"] = "AGGREGATE_INTERVAL_MISSING"
            return out
        n_blk = sum(1 for c in series["a_count" if paired else "count"]
                    if isinstance(c, int) and not isinstance(c, bool) and c >= 1)
        err = g._influence_series_error(series, diff=paired, expected_a_blocks=n_blk)
        if err is not None:
            out["status"] = err
            return out
        sc = inf.influence_scores(series, diff=paired)
        if sc is None:
            out["status"] = "AGGREGATE_SERIES_INCONSISTENT"
            return out
        ids, psi, point = sc
        points.append(point)
        lo, hi = ci_from_series(series, diff=paired, samples=samples) if n_blk >= min_blocks else (None, None)
        stored = (iv or {}).get("ci") or [None, None]
        rec = {"block_ms": ms, "resampling_blocks": n_blk, "ci": [lo, hi], "stored_ci": list(stored),
               "stored_matches": (lo is not None and len(stored) == 2 and stored[0] is not None
                                  and stored[1] is not None
                                  and abs(float(stored[0]) - lo) <= CI_COMPARISON_TOLERANCE
                                  and abs(float(stored[1]) - hi) <= CI_COMPARISON_TOLERANCE)}
        out["intervals"].append(rec)
        if n_blk >= min_blocks and lo is not None and hi is not None:
            usable.append((ms, lo, hi, ids, psi))
    if max(points) - min(points) > g.AGGREGATE_POINT_TOLERANCE:
        out["status"] = "AGGREGATE_UNIVERSES_INCONSISTENT"
        return out
    out["point_estimate"] = points[0]
    if not usable:
        out["status"] = "INSUFFICIENT_RESAMPLING_BLOCKS"
    else:
        _, _, _, ids, psi = max(usable, key=lambda x: x[0])
        res = inf.acf_from_block_series(ids, psi)
        out["residual_dependence"] = {"estimable": res["estimable"], "significant": res["significant"],
                                      "acf": res["acf"], "not_estimable_reason": res["not_estimable_reason"]}
        if not res["estimable"]:
            out["status"] = inf.DIFF_RESIDUAL_NOT_ESTIMABLE if paired else inf.RESIDUAL_NOT_ESTIMABLE
        elif res["significant"]:
            out["status"] = inf.DIFF_RESIDUAL_DEPENDENCE if paired else inf.RESIDUAL_DEPENDENCE
        else:
            out["status"] = "VALID"
            out["ci"] = [min(u[1] for u in usable), max(u[2] for u in usable)]
    out["stored_comparison"] = {
        "authority_status": (section or {}).get("authority_status"),
        "authority_ci": [(section or {}).get("authority_ci_low"), (section or {}).get("authority_ci_high")],
        "all_interval_cis_match": all(r["stored_matches"] for r in out["intervals"] if r["ci"][0] is not None),
        "role": "COMPARISON_ONLY",
    }
    return out
