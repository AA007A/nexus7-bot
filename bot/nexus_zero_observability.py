"""Observability for zero-valued NEXUS vetoes.

A NexusDecision.wait() created before the final score/fusion stage legitimately
carries setup_quality=0 and confidence=0. Those zeros must not be mistaken for
a broken model. This module classifies the veto stage from existing reasoning
without changing the decision object, thresholds, risk, or execution.
"""


def _classify_reason(reason: str) -> str:
    text = (reason or "").lower()
    if any(k in text for k in ("qualidade de dados", "candles", "dados insuficientes", "dados antigos", "data_unavailable")):
        return "DATA_GATE"
    if any(k in text for k in ("conflito entre timeframes", "diverge do mtf", "mtf")):
        return "MTF_GATE"
    if any(k in text for k in ("extreme_event", "incompatível com regime", "regime")):
        return "REGIME_GATE"
    if any(k in text for k in ("ensemble sem direção", "ensemble (", "modelos")):
        return "ENSEMBLE_GATE"
    if any(k in text for k in ("entrada/sl/tp", "stop inválido", "níveis de entrada")):
        return "LEVELS_GATE"
    if any(k in text for k in ("ev negativo", "r:r líquido")):
        return "EV_RR_GATE"
    if "score" in text:
        return "FINAL_SCORE_GATE"
    return "EARLY_VETO_OTHER"


def observe(decision, log):
    """Log a precise cause when a veto has both score and confidence at zero."""
    try:
        if getattr(decision, "execution_allowed", False) is True:
            return
        score = float(getattr(decision, "setup_quality", 0.0) or 0.0)
        confidence = float(getattr(decision, "confidence", 0.0) or 0.0)
        if score != 0.0 or confidence != 0.0:
            return
        reasoning = getattr(decision, "reasoning", None) or []
        reason = str(reasoning[-1]) if reasoning else "sem motivo"
        stage = _classify_reason(reason)
        data_quality = float(getattr(decision, "data_quality", 0.0) or 0.0)
        symbol = getattr(decision, "symbol", "?")
        log.info(
            "[NEXUS_ZERO] symbol=%s stage=%s score=0 confidence=0 "
            "meaning=pre_final_score_veto data_quality=%.1f reason=%s",
            symbol, stage, data_quality, reason[:180],
        )
    except Exception as exc:
        log.debug("[NEXUS_ZERO] observability failed: %s: %s", type(exc).__name__, exc)
