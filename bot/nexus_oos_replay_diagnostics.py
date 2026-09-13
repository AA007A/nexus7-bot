"""Diagnostic companion for the real NEXUS OOS replay.

Analytics-only. It instruments the final NEXUS decision callable after runtime
hardening is installed and summarizes why historical candidates are approved or
rejected. It does not alter exchange state or live runtime configuration.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path


def _category(reason: str, warnings: list[str] | None = None) -> str:
    warning_text = " | ".join(str(x) for x in (warnings or []))
    if "CRITICAL_CANDLE_INTEGRITY:" in warning_text:
        detail = warning_text.split("CRITICAL_CANDLE_INTEGRITY:", 1)[1].split(" | ", 1)[0]
        return "CANDLE_INTEGRITY:" + detail[:120]
    text = str(reason or "")
    if "REJEITADO: score" in text:
        return "SCORE_BELOW_THRESHOLD"
    if "diverge do MTF" in text:
        return "ENSEMBLE_MTF_DIVERGENCE"
    if "Conflito entre timeframes" in text:
        return "MTF_CONFLICT"
    if "incompatível com regime" in text:
        return "REGIME_INCOMPATIBLE"
    if "Ensemble sem direção" in text:
        return "ENSEMBLE_NO_DIRECTION"
    if "EV negativo" in text:
        return "NEGATIVE_EV"
    if "R:R líquido" in text:
        return "RR_NET_BELOW_MIN"
    if "Qualidade de dados" in text:
        return "DATA_QUALITY"
    if "Stop inválido" in text:
        return "STOP_GEOMETRY"
    if "Níveis de entrada/SL/TP" in text:
        return "MISSING_LEVELS"
    if "EXTREME_EVENT" in text:
        return "EXTREME_EVENT"
    return text[:160] if text else "UNKNOWN"


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "min": None, "max": None, "mean": None}
    return {"n": len(values), "min": min(values), "max": max(values), "mean": sum(values) / len(values)}


async def run(symbols: list[str], limit_15m: int) -> dict:
    from bot.runtime_bootstrap import install as install_runtime
    install_runtime()

    from bot import nexus_ai
    from bot.nexus_oos_real_replay import run_real_replay

    original = nexus_ai.decide
    records: list[dict] = []

    def instrumented(*args, **kwargs):
        decision = original(*args, **kwargs)
        reasoning = list(getattr(decision, "reasoning", []) or [])
        warnings = [str(x) for x in (getattr(decision, "warnings", []) or [])]
        reason = reasoning[-1] if reasoning else ""
        records.append({
            "symbol": str(getattr(decision, "symbol", "")),
            "approved": getattr(decision, "execution_allowed", False) is True,
            "decision": str(getattr(decision, "decision", "")),
            "reason": str(reason),
            "warnings": warnings,
            "category": _category(reason, warnings),
            "setup_quality": float(getattr(decision, "setup_quality", 0.0) or 0.0),
            "data_quality": float(getattr(decision, "data_quality", 0.0) or 0.0),
            "confidence": float(getattr(decision, "confidence", 0.0) or 0.0),
            "risk_reward": float(getattr(decision, "risk_reward", 0.0) or 0.0),
            "expected_value": float(getattr(decision, "expected_value", 0.0) or 0.0),
        })
        return decision

    nexus_ai.decide = instrumented
    try:
        report = await run_real_replay(symbols, limit_15m=limit_15m)
    finally:
        nexus_ai.decide = original

    categories = Counter(r["category"] for r in records if not r["approved"])
    approved = [r for r in records if r["approved"]]
    rejected = [r for r in records if not r["approved"]]
    report["decision_diagnostics"] = {
        "observed_decisions": len(records),
        "approved": len(approved),
        "rejected": len(rejected),
        "rejection_categories": dict(categories.most_common()),
        "setup_quality_all": _stats([r["setup_quality"] for r in records]),
        "setup_quality_rejected": _stats([r["setup_quality"] for r in rejected]),
        "data_quality_all": _stats([r["data_quality"] for r in records]),
        "confidence_all": _stats([r["confidence"] for r in records]),
        "risk_reward_all": _stats([r["risk_reward"] for r in records]),
        "expected_value_all": _stats([r["expected_value"] for r in records]),
        "sample_rejections": rejected[:20],
        "sample_approvals": approved[:20],
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"])
    parser.add_argument("--limit-15m", type=int, default=1600)
    parser.add_argument("--output", default="artifacts/nexus_oos_replay_diagnostics.json")
    args = parser.parse_args()
    report = asyncio.run(run(args.symbols, args.limit_15m))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report["decision_diagnostics"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
