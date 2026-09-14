"""Generate primary and robustness NEXUS OOS evidence from one replay sample.

Research only. The command installs the production runtime wrapper stack in a
PAPER-safe process, fetches each symbol exactly once through the canonical
contiguous-history/full-clock replay adapter, and derives both the primary edge
report and robustness diagnostics from that same in-memory candidate population.
This prevents time-window or runtime-stack drift between separate research runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from bot.nexus_oos_edge_gate import build_edge_report, edge_promotion_decision
from bot.nexus_oos_real_replay import PublicKuCoinFuturesClient
from bot.nexus_oos_real_replay_corrected import replay_symbol
from bot.nexus_oos_robustness import analyze_robustness
from bot.nexus_oos_temporal_block_bootstrap import temporal_block_bootstrap_dict


REPLAY_SOURCE = "nexus_oos_real_replay_corrected"
RUNTIME_STACK = "runtime_bootstrap.install"


async def collect(symbols: Iterable[str], *, limit_15m: int) -> list[dict]:
    from bot.runtime_bootstrap import install as install_runtime

    install_runtime()
    reports: list[dict] = []
    async with PublicKuCoinFuturesClient() as client:
        for symbol in symbols:
            reports.append(await replay_symbol(client, str(symbol), limit_15m=limit_15m))
    return reports


def _all_rows(symbol_reports: list[dict]) -> list:
    return [
        row
        for rep in symbol_reports
        for row in (rep.get("candidates", []) or [])
    ]


def build_primary_report(symbol_reports: list[dict]) -> dict:
    all_rows = _all_rows(symbol_reports)
    edge = build_edge_report(all_rows)
    statistically_ok, blockers = edge_promotion_decision(edge)
    valid_reports = [rep for rep in symbol_reports if "error" not in rep]
    context_parity_complete = bool(valid_reports) and all(
        bool(rep.get("historical_context", {}).get("parity_complete"))
        for rep in valid_reports
    )
    final_blockers = list(blockers)
    if not context_parity_complete:
        final_blockers.append("HISTORICAL_CONTEXT_PARITY_INCOMPLETE")

    compact_symbols = []
    for rep in symbol_reports:
        compact = {k: v for k, v in rep.items() if k != "candidates"}
        compact["candidate_count"] = len(rep.get("candidates", []) or [])
        compact_symbols.append(compact)

    return {
        "status": (
            "AI_EDGE_PROVEN"
            if statistically_ok and context_parity_complete
            else "AI_EDGE_NOT_PROVEN"
        ),
        "blockers": sorted(set(final_blockers)),
        "report": asdict(edge),
        "symbols": compact_symbols,
        "methodology": {
            "closed_candles_only": True,
            "historical_clock_frozen": True,
            "same_bar_ambiguity": "STOP_FIRST",
            "fees_included": True,
            "slippage_included": True,
            "funding_included_when_public_history_available": True,
            "exchange_mutations": False,
            "runtime_policy_mutations": False,
            "replay_source": REPLAY_SOURCE,
            "runtime_stack": RUNTIME_STACK,
            "shared_candidate_population": True,
        },
    }


def build_robustness_bundle(symbol_reports: list[dict]) -> dict:
    all_rows = _all_rows(symbol_reports)
    robustness = analyze_robustness(symbol_reports, temporal_folds=4)
    temporal_dependence = temporal_block_bootstrap_dict(
        all_rows,
        bucket_seconds=86_400,
        block_buckets=3,
        bootstrap_samples=4_000,
        seed=29,
        min_unique_buckets=8,
    )
    valid_reports = [rep for rep in symbol_reports if "error" not in rep]
    compact_symbols = [
        {
            "symbol": rep.get("symbol"),
            "candidate_count": len(rep.get("candidates", []) or []),
            "approved": rep.get("approved", 0),
            "rejected": rep.get("rejected", 0),
            "historical_context": rep.get("historical_context", {}),
            "error": rep.get("error"),
        }
        for rep in symbol_reports
    ]
    return {
        "status": "ROBUSTNESS_RESEARCH_ONLY",
        "replay_source": REPLAY_SOURCE,
        "runtime_stack": RUNTIME_STACK,
        "shared_candidate_population": True,
        "symbols": compact_symbols,
        "robustness": robustness,
        "temporal_block_bootstrap": temporal_dependence,
        "historical_context_parity_complete": bool(valid_reports) and all(
            bool(rep.get("historical_context", {}).get("parity_complete"))
            for rep in valid_reports
        ),
        "promotion_authority": False,
        "execution_effect": "NONE",
    }


def assert_population_parity(primary: dict, robustness: dict) -> None:
    primary_symbols = {
        item["symbol"]: int(item.get("candidate_count", 0))
        for item in primary.get("symbols", [])
    }
    robust_symbols = {
        item["symbol"]: int(item.get("candidate_count", 0))
        for item in robustness.get("symbols", [])
    }
    if primary_symbols != robust_symbols:
        raise RuntimeError(
            f"OOS candidate population drift: primary={primary_symbols} robustness={robust_symbols}"
        )
    primary_total = int(primary.get("report", {}).get("baseline_candidates", 0))
    robust_total = int(
        robustness.get("robustness", {}).get("pooled", {}).get("baseline_candidates", 0)
    )
    block_total = int(
        robustness.get("temporal_block_bootstrap", {}).get("known_baseline_outcomes", 0)
    )
    if (
        primary_total != robust_total
        or primary_total != sum(primary_symbols.values())
        or block_total != primary_total
    ):
        raise RuntimeError(
            f"OOS candidate total drift: primary={primary_total} robustness={robust_total} "
            f"temporal_block={block_total} symbols={sum(primary_symbols.values())}"
        )


async def run(symbols: list[str], *, limit_15m: int) -> tuple[dict, dict]:
    reports = await collect(symbols, limit_15m=limit_15m)
    primary = build_primary_report(reports)
    robustness = build_robustness_bundle(reports)
    assert_population_parity(primary, robustness)
    return primary, robustness


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"],
    )
    parser.add_argument("--limit-15m", type=int, default=3000)
    parser.add_argument("--primary-output", default="artifacts/nexus_oos_real_replay.json")
    parser.add_argument("--robustness-output", default="artifacts/nexus_oos_robustness.json")
    args = parser.parse_args()

    primary, robustness = asyncio.run(run(list(args.symbols), limit_15m=int(args.limit_15m)))
    primary_out = Path(args.primary_output)
    robustness_out = Path(args.robustness_output)
    primary_out.parent.mkdir(parents=True, exist_ok=True)
    robustness_out.parent.mkdir(parents=True, exist_ok=True)
    primary_out.write_text(json.dumps(primary, indent=2, sort_keys=True), encoding="utf-8")
    robustness_out.write_text(json.dumps(robustness, indent=2, sort_keys=True), encoding="utf-8")

    pooled = robustness["robustness"].get("pooled") or {}
    summary = robustness["robustness"]["summary"]
    block = robustness.get("temporal_block_bootstrap", {})
    print(json.dumps({
        "status": primary["status"],
        "blockers": primary["blockers"],
        "candidate_count": primary["report"]["baseline_candidates"],
        "approved_candidates": primary["report"]["approved_candidates"],
        "rejected_candidates": primary["report"]["rejected_candidates"],
        "expectancy_uplift_r": primary["report"]["expectancy_uplift_r"],
        "bootstrap_ci_low_r": primary["report"]["bootstrap_ci_low_r"],
        "bootstrap_ci_high_r": primary["report"]["bootstrap_ci_high_r"],
        "robustness_candidate_count": pooled.get("baseline_candidates"),
        "stable_positive_point_estimate": summary.get("stable_positive_point_estimate"),
        "temporal_block_available": block.get("available"),
        "temporal_block_ci_low_r": block.get("ci_low_r"),
        "temporal_block_ci_high_r": block.get("ci_high_r"),
        "temporal_block_ci_strictly_positive": block.get("ci_strictly_positive"),
        "shared_candidate_population": True,
        "promotion_authority": False,
        "execution_effect": "NONE",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
