"""Run read-only historical NEXUS replay and robustness decomposition.

This command uses KuCoin public endpoints only and has no trading authority.
It reuses the leakage-aware replay implementation, then evaluates symbol,
chronological-fold and leave-one-symbol-out stability.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from bot.nexus_oos_real_replay import PublicKuCoinFuturesClient, replay_symbol
from bot.nexus_oos_robustness import analyze_robustness


async def run(symbols: list[str], *, limit_15m: int) -> dict:
    # Runtime wrappers are installed in PAPER-safe research mode by the same
    # replay stack. Only public market-data endpoints are used below.
    reports = []
    async with PublicKuCoinFuturesClient() as client:
        for symbol in symbols:
            reports.append(await replay_symbol(client, symbol, limit_15m=limit_15m))

    robustness = analyze_robustness(reports, temporal_folds=4)
    compact_symbols = []
    for rep in reports:
        compact_symbols.append({
            "symbol": rep.get("symbol"),
            "candidate_count": len(rep.get("candidates", []) or []),
            "approved": rep.get("approved", 0),
            "rejected": rep.get("rejected", 0),
            "historical_context": rep.get("historical_context", {}),
            "error": rep.get("error"),
        })

    return {
        "status": "ROBUSTNESS_RESEARCH_ONLY",
        "symbols": compact_symbols,
        "robustness": robustness,
        "historical_context_parity_complete": all(
            bool(rep.get("historical_context", {}).get("parity_complete"))
            for rep in reports if not rep.get("error")
        ) and bool(reports),
        "promotion_authority": False,
        "execution_effect": "NONE",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"],
    )
    parser.add_argument("--limit-15m", type=int, default=3000)
    parser.add_argument("--output", default="artifacts/nexus_oos_robustness.json")
    args = parser.parse_args()

    report = asyncio.run(run(list(args.symbols), limit_15m=int(args.limit_15m)))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    summary = report["robustness"]["summary"]
    pooled = report["robustness"].get("pooled") or {}
    print(json.dumps({
        "status": report["status"],
        "historical_context_parity_complete": report["historical_context_parity_complete"],
        "candidate_count": pooled.get("baseline_candidates", 0),
        "uplift_r": pooled.get("expectancy_uplift_r"),
        "ci_low_r": pooled.get("bootstrap_ci_low_r"),
        "ci_high_r": pooled.get("bootstrap_ci_high_r"),
        "stable_positive_point_estimate": summary.get("stable_positive_point_estimate"),
        "symbols_positive": summary.get("symbols_positive_uplift"),
        "temporal_folds_positive": summary.get("temporal_folds_positive_uplift"),
        "leave_one_out_positive": summary.get("leave_one_symbol_out_positive_uplift"),
        "promotion_authority": False,
        "execution_effect": "NONE",
        "output": str(out),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
