"""Public-market 12-symbol NEXUS OOS evidence batch.

Research-only entrypoint. Uses only KuCoin public market endpoints, never reads
private account credentials, and never calls an exchange mutation method.

The batch composes the corrected historical replay with the portfolio evidence
aggregator. Portfolio inference bootstraps complete decision-timestamp clusters
so simultaneous cross-asset candidates remain dependent. Because historical OI
and order-book snapshots are not available in the current public replay, this
entrypoint is fail-closed: it always retains HISTORICAL_CONTEXT_PARITY_INCOMPLETE
until every successful symbol explicitly proves full context parity.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Iterable

from bot.nexus_oos_portfolio import write_portfolio_evidence_bundle
from bot.nexus_oos_real_replay import PublicKuCoinFuturesClient
from bot.nexus_oos_real_replay_corrected import replay_symbol
from bot.nexus_oos_replay import ReplayEvidence

DEFAULT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
    "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT",
)


def _to_replay_evidence(report: dict) -> ReplayEvidence:
    symbol = str(report.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("symbol report missing symbol")
    candidates = tuple(report.get("candidates") or ())
    return ReplayEvidence(
        symbol=symbol,
        candidates=candidates,
        baseline_trade_count=len(candidates),
        evaluated_count=len(candidates),
        warmup_excluded_count=0,
        parity=(
            "FULL_CONTEXT"
            if bool(report.get("historical_context", {}).get("parity_complete"))
            else "CORE_CANDLES_ONLY"
        ),
    )


def _compact_symbol(report: dict) -> dict:
    return {
        key: value
        for key, value in report.items()
        if key != "candidates"
    } | {"candidate_count": len(report.get("candidates") or ())}


async def run_public_batch(
    symbols: Iterable[str] = DEFAULT_SYMBOLS,
    *,
    limit_15m: int = 2500,
    output_dir: str | Path = "artifacts/nexus_oos_public",
    require_all_symbols: bool = True,
    bootstrap_samples: int = 4000,
    seed: int = 7,
) -> dict:
    requested = tuple(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
    if not requested:
        raise ValueError("at least one symbol is required")

    from bot.runtime_bootstrap import install as install_runtime
    install_runtime()

    reports: list[dict] = []
    failures: list[dict] = []
    replays: list[ReplayEvidence] = []

    async with PublicKuCoinFuturesClient() as client:
        for symbol in requested:
            try:
                report = await replay_symbol(client, symbol, limit_15m=int(limit_15m))
            except Exception as exc:
                report = {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}", "candidates": []}
            reports.append(report)
            if report.get("error"):
                failures.append({"symbol": symbol, "error": str(report.get("error"))})
                continue
            replays.append(_to_replay_evidence(report))

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    if replays:
        payload = write_portfolio_evidence_bundle(
            replays,
            destination,
            bootstrap_samples=int(bootstrap_samples),
            seed=int(seed),
        )
    else:
        payload = {
            "schema_version": 1,
            "status": "AI_EDGE_NOT_PROVEN",
            "proven": False,
            "blockers": ["NO_REPLAY_DATA"],
            "portfolio_report": {},
            "per_symbol": {},
            "parity": "CORE_CANDLES_ONLY",
            "bootstrap_unit": "decision_timestamp_cluster",
        }

    blockers = set(payload.get("blockers") or ())
    parity_complete = bool(replays) and all(
        bool(report.get("historical_context", {}).get("parity_complete"))
        for report in reports if not report.get("error")
    )
    if not parity_complete:
        blockers.add("HISTORICAL_CONTEXT_PARITY_INCOMPLETE")
    if failures and require_all_symbols:
        blockers.add("REQUESTED_SYMBOL_REPLAY_INCOMPLETE")

    # Full context parity is a promotion prerequisite. Statistical positivity
    # alone can never override missing historical production inputs.
    payload["proven"] = False if blockers else bool(payload.get("proven"))
    payload["status"] = "AI_EDGE_PROVEN" if payload["proven"] else "AI_EDGE_NOT_PROVEN"
    payload["blockers"] = sorted(blockers)
    payload["historical_context_parity_complete"] = parity_complete
    payload["requested_symbols"] = list(requested)
    payload["successful_symbols"] = [replay.symbol for replay in replays]
    payload["failed_symbols"] = failures
    payload["symbols"] = [_compact_symbol(report) for report in reports]
    payload["execution_effect"] = "NONE"
    payload["private_credentials_required"] = False
    payload["methodology"] = {
        "closed_candles_only": True,
        "corrected_contiguous_public_history": True,
        "historical_clock_frozen": True,
        "fees_included": True,
        "slippage_included": True,
        "funding_included_when_public_history_available": True,
        "portfolio_bootstrap_unit": "decision_timestamp_cluster",
        "historical_open_interest": False,
        "historical_orderbook": False,
        "exchange_mutations": False,
        "runtime_policy_mutations": False,
    }

    report_path = destination / "nexus_oos_portfolio_report.json"
    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--limit-15m", type=int, default=2500)
    parser.add_argument("--output-dir", default="artifacts/nexus_oos_public")
    parser.add_argument("--bootstrap-samples", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    payload = asyncio.run(
        run_public_batch(
            args.symbols,
            limit_15m=args.limit_15m,
            output_dir=args.output_dir,
            require_all_symbols=not args.allow_partial,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed,
        )
    )
    print(json.dumps({
        "status": payload.get("status"),
        "blockers": payload.get("blockers"),
        "requested_symbols": payload.get("requested_symbols"),
        "successful_symbols": payload.get("successful_symbols"),
        "failed_symbols": payload.get("failed_symbols"),
        "portfolio_report": payload.get("portfolio_report"),
        "historical_context_parity_complete": payload.get("historical_context_parity_complete"),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
