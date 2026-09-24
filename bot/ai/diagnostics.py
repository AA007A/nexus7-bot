"""Root-cause decomposition of losses (research only; no decision authority).

Decomposes executable, RESOLVED production-parity outcomes by symbol,
direction, UTC hour, weekday, regime, volatility / score / confidence buckets,
entry type, RR bucket, holding duration, exit reason and cost components, and
answers the Phase-7 questions with numbers rather than opinions.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone


def _stats(v):
    n = len(v)
    if not n:
        return {"n": 0}
    return {"n": n, "mean_r": sum(v) / n, "total_r": sum(v), "win_rate": sum(1 for x in v if x > 0) / n}


def _group(rows, key_fn):
    g = defaultdict(list)
    for r in rows:
        g[str(key_fn(r))].append(float(r["r"]))
    return {k: _stats(v) for k, v in sorted(g.items())}


def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    rk = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            rk[order[k]] = (i + j) / 2.0
        i = j + 1
    return rk


def spearman(a, b):
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 10:
        return None
    ra, rb = _rank([p[0] for p in pairs]), _rank([p[1] for p in pairs])
    n = len(pairs)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return cov / (va * vb) if va > 0 and vb > 0 else None


def _hour(r):
    return datetime.fromtimestamp(int(r["ts"]) / 1000, tz=timezone.utc)


def _bucket(x, edges):
    if x is None:
        return "NA"
    for e in edges:
        if x < e:
            return f"<{e}"
    return f">={edges[-1]}"


def decomposition(rows) -> dict:
    res = [r for r in rows if r.get("executable") and r.get("outcome_status") == "RESOLVED"
           and r.get("r") is not None]
    appr = [r for r in res if r.get("approved")]

    def hold_h(r):
        h = r.get("outcome_horizon_ms")
        return None if h is None else h / 3_600_000

    out = {"population": {"executable_resolved": len(res), "approved": len(appr)}}
    for name, pop in (("baseline", res), ("approved", appr)):
        out[name] = {
            "by_symbol": _group(pop, lambda r: r.get("symbol")),
            "by_direction": _group(pop, lambda r: r.get("direction")),
            "by_utc_hour": _group(pop, lambda r: _hour(r).hour),
            "by_weekday": _group(pop, lambda r: _hour(r).strftime("%a")),
            "by_regime": _group(pop, lambda r: r.get("ai_regime") or r.get("research_regime")),
            "by_volatility_bucket": _group(pop, lambda r: r.get("volatility_bucket")),
            "by_nexus_score_bucket": _group(pop, lambda r: r.get("score_bucket")),
            "by_confidence_bucket": _group(pop, lambda r: r.get("confidence_bucket")),
            "by_entry_type": _group(pop, lambda r: r.get("entry_type")),
            "by_rr_bucket": _group(pop, lambda r: _bucket(r.get("rr"), (1.5, 2.0, 2.5, 3.0))),
            "by_holding_hours": _group(pop, lambda r: _bucket(hold_h(r), (1, 4, 12, 24))),
            "by_exit_reason": _group(pop, lambda r: r.get("exit_reason")),
            "costs_r": {k: (sum(float(r.get(k) or 0.0) for r in pop) / len(pop) if pop else None)
                        for k in ("gross_r", "fees_r", "slippage_r", "funding_r", "r")},
        }
    a = out["approved"]
    gross = a["costs_r"]["gross_r"]
    net = a["costs_r"]["r"]
    exits = a["by_exit_reason"]
    sl_share = (sum(v["n"] for k, v in exits.items() if "SL" in k.upper() or "STOP" in k.upper())
                / len(appr)) if appr else None
    dirs = a["by_direction"]
    regs = sorted(((k, v.get("total_r", 0.0)) for k, v in a["by_regime"].items()), key=lambda t: t[1])
    total_loss = sum(min(0.0, t) for _, t in regs)
    out["answers"] = {
        "entries_wrong_gross_negative": (gross is not None and gross < 0),
        "mean_gross_r_approved": gross, "mean_net_r_approved": net,
        "costs_consume_edge": (gross is not None and net is not None and gross > 0 >= net),
        "stop_exit_share_approved": sl_share,
        "short_structurally_worse": ((dirs.get("SHORT", {}).get("mean_r") or 0)
                                     < (dirs.get("LONG", {}).get("mean_r") or 0)) if dirs else None,
        "worst_regime": regs[0][0] if regs else None,
        "worst_regime_share_of_losses": (regs[0][1] / total_loss) if regs and total_loss < 0 else None,
        "nexus_score_rank_correlation_with_r": spearman(
            [r.get("nexus_score") for r in res], [float(r["r"]) for r in res]),
        "nexus_confidence_rank_correlation_with_r": spearman(
            [r.get("nexus_confidence") for r in res], [float(r["r"]) for r in res]),
    }
    return out
