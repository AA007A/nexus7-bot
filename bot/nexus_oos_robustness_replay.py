"""Run read-only historical NEXUS replay and robustness decomposition.

This command uses KuCoin public endpoints only and has no trading authority.
It deliberately imports the canonical *corrected* replay adapter and installs
the same production runtime wrapper stack as the primary OOS replay before any
candidate is evaluated. This keeps the robustness population methodologically
identical to the primary report while remaining PAPER-safe/research-only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from bot.nexus_oos_real_replay import PublicKuCoinFuturesClient
from bot.nexus_oos_real_replay_corrected import replay_symbol
from bot.nexus_oos_robustness import analyze_robustness


REPLAY_SOURCE = "nexus_oos_real_replay_corrected"
RUNTIME_STACK = "runtime_bootstrap.install"


async def run(symbols: list[str], *, limit_15m: int) -> dict:
    # Match bot.nexus_oos_real_replay.run_real_replay exactly: the production
    # wrapper stack is installed in a PAPER-safe process before replaying any
    # symbol. No exchange mutation method is invoked by this research path.
    from bot.runtime_bootstrap import install as install_runtime
    install_runtime()

    reports = []
    async with PublicKuCoinFuturesClient() as client:
        for symbol in symbols:
            reports.append(await replay_symbol(client, symbol, limit_15m=limit_15m, research=False))

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
        "replay_source": REPLAY_SOURCE,
        "runtime_stack": RUNTIME_STACK,
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
        "replay_source": report["replay_source"],
        "runtime_stack": report["runtime_stack"],
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
