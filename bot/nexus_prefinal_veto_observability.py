"""Read-only model evidence for NEXUS decisions that terminate before final score.

Some NEXUS vetoes happen before ``_score_components`` (for example MTF/ensemble
divergence). In those cases the final score is intentionally unavailable, but
operators still need to know which ensemble models produced the opposing vote.

This module logs the model snapshot already attached to ``NexusDecision``. It
never changes the decision, threshold, score, risk state, or exchange behavior.
"""
from __future__ import annotations

from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _compact_models(models) -> list[dict]:
    out = []
    for item in models or []:
        if isinstance(item, dict):
            name = str(item.get("name", "UNKNOWN"))
            direction = str(item.get("direction", "WAIT"))
            confidence = _safe_float(item.get("confidence"))
            risk = _safe_float(item.get("risk"))
            available = bool(item.get("available", True))
            reason = str(item.get("reason", ""))
        else:
            name = str(getattr(item, "name", "UNKNOWN"))
            direction = str(getattr(getattr(item, "direction", "WAIT"), "value",
                                    getattr(item, "direction", "WAIT")))
            confidence = _safe_float(getattr(item, "confidence", 0.0))
            risk = _safe_float(getattr(item, "risk_score", 0.0))
            available = bool(getattr(item, "available", True))
            reason = str(getattr(item, "reason", ""))
        out.append({
            "name": name,
            "direction": direction,
            "confidence": round(confidence, 1),
            "risk": round(risk, 1),
            "available": available,
            "reason": reason[:160],
        })
    return out


def _terminal_reason(decision) -> str:
    reasoning = list(getattr(decision, "reasoning", []) or [])
    return str(reasoning[-1]) if reasoning else ""


def install(nexus_ai, log) -> None:
    if getattr(nexus_ai, "_prefinal_veto_observability_installed", False):
        return

    original_decide = nexus_ai.decide

    def decide_with_prefinal_models(*args, **kwargs):
        decision = original_decide(*args, **kwargs)
        models = _compact_models(getattr(decision, "models", None))
        terminal = _terminal_reason(decision)
        setup_quality = _safe_float(getattr(decision, "setup_quality", 0.0))
        allowed = bool(getattr(decision, "execution_allowed", False))

        # A true final-score rejection already has rich decomposition telemetry.
        # This line is most valuable when the score is absent/zero because a
        # prior gate ended the decision before score construction.
        if not allowed and (setup_quality <= 0.0 or not models):
            log.info(
                "[NEXUS_PREFINAL_VETO_MODELS] symbol=%s decision=%s "
                "setup_quality=%s reason=%s models=%s execution_effect=NONE",
                getattr(decision, "symbol", kwargs.get("symbol", "UNKNOWN")),
                getattr(decision, "decision", "WAIT"),
                round(setup_quality, 2),
                terminal,
                models,
            )
        return decision

    nexus_ai.decide = decide_with_prefinal_models
    nexus_ai._prefinal_veto_observability_installed = True
    log.info(
        "[NEXUS_PREFINAL_VETO_OBSERVABILITY] installed "
        "model_snapshot_on_early_veto=true decision_effect=NONE execution_effect=NONE"
    )
