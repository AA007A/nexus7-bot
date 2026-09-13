"""Non-blocking coverage telemetry for market-risk intelligence.

This module is deliberately observational. It annotates the existing
``market_risk_runtime.snapshot()`` result with provider-coverage health without
changing the risk assessment, execution authorization, position sizing,
leverage, or order-routing behavior.

The distinction matters operationally: ``assessment.level == NORMAL`` means
that the *available fresh evidence* is not dislocated. It must not be confused
with "all external providers are healthy". A separate ``coverage_state`` makes
that distinction explicit while preserving the operator-requested fail-neutral
execution policy.
"""
from __future__ import annotations

from typing import Any, Mapping


_DEGRADED_TOKENS = (
    "unavailable",
    "stale",
    "fail_neutral",
    "no_valid_signals",
    "http_4",
    "http_5",
    "api=['401'",
    'api=["401"',
)


def _provider_degraded(name: str, status: Any) -> bool:
    text = str(status or "").strip().lower()
    if not text:
        return False

    # CoinGlass is explicitly optional. A deliberately unconfigured optional
    # provider is not an outage; authentication/HTTP/parser failures are.
    if name == "coinglass_v4" and text == "disabled_no_key":
        return False

    if text.startswith("ok"):
        return False
    if text.startswith("partial"):
        return True
    return any(token in text for token in _DEGRADED_TOKENS)


def classify_coverage(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return coverage metadata without any execution authority."""
    snap = snapshot or {}
    providers = dict(snap.get("providers", {}) or {})
    stale = dict(snap.get("stale_reasons", {}) or {})
    signals = dict(snap.get("signals", {}) or {})

    degraded_providers = tuple(sorted(
        name for name, status in providers.items()
        if _provider_degraded(str(name), status)
    ))
    reasons: list[str] = []
    if degraded_providers:
        reasons.append("PROVIDER_DEGRADED")
    if stale:
        reasons.append("STALE_SIGNALS")
    if not signals:
        reasons.append("NO_FRESH_EXTERNAL_SIGNALS")

    state = "DEGRADED" if reasons else "NORMAL"
    return {
        "coverage_state": state,
        "coverage_reasons": tuple(reasons),
        "degraded_providers": degraded_providers,
        "fresh_signal_count": len(signals),
        "coverage_execution_effect": "NONE",
    }


def install(runtime_module, log) -> None:
    """Annotate runtime snapshots; never alter the contained assessment."""
    if getattr(runtime_module, "_coverage_observability_installed", False):
        return

    original_snapshot = runtime_module.snapshot

    def snapshot_with_coverage(*args, **kwargs):
        snap = original_snapshot(*args, **kwargs)
        if not isinstance(snap, dict):
            return snap
        out = dict(snap)
        out.update(classify_coverage(out))
        return out

    runtime_module.snapshot = snapshot_with_coverage
    runtime_module._coverage_observability_installed = True
    log.info(
        "[MARKET_RISK_COVERAGE] installed telemetry_only=true "
        "execution_effect=NONE thresholds_unchanged=true leverage_unchanged=true "
        "sizing_unchanged=true"
    )
