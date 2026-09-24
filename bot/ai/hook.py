"""AI_RUNTIME_HOOK_POPULATION_V1 — the exact state immediately before
``TradingEngine._ai_gate(sig, nx_dec)``.

The AI sees only candidates that reached its runtime call site, carrying the
geometry, score and NEXUS decision the engine holds AT THAT POINT:

  _scan_all_and_enter
    analyze_mtf -> sig                                   (strategy candidate)
    sig.score = session-adjusted score; < effective min  -> dropped
    regime must allow direction                          -> else dropped
    sig.expected_pnl > 0                                 -> else dropped
  _open
    viable_symbols / durable state                       (infrastructure; assumed healthy)
    nx_dec = _nexus_validate(sig)
      LIVE pilot profile (paper_trade False, pilot on): the CROSS wrapper
        runs NEXUS, then exact-MMR geometry: BLOCK -> wait (not approved);
        ADJUSTED -> sig.sl/tp/rr mutated and NEXUS re-run (revised decision)
      PAPER / non-pilot profile: the initial NEXUS decision, original geometry
    nx_dec.execution_allowed is True                     (validation passed)
  --> _ai_gate(sig, nx_dec)   <== HOOK

Profiles are explicit and versioned. The training population is the
LIVE_PILOT profile (the only profile whose outcome model is the replay's
production-parity outcome). A bundle trained on one profile never becomes
authoritative on another (runtime fails closed, SHADOW records the mismatch).

Stateful pre-hook gates (effective min score after the daily target, same
symbol / correlation / cooldown / circuit breaker, MAX_POSITIONS) depend on
portfolio state: candidate-level eligibility is STATELESS, and the portfolio
replay applies the stateful gates.

Downstream of the hook the engine still runs deterministic gates (balance,
pilot guards, the legacy scoring.calculate pre-score, sizing, liquidation,
pre-dispatch drift/spread/depth). The replay does not reproduce
scoring.calculate or historical order flow / macro / news, so
AI_EFFECTIVE_EXECUTION_PARITY is INCOMPLETE.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

POPULATION = "AI_RUNTIME_HOOK_POPULATION_V1"
PROFILE_LIVE_PILOT = "LIVE_PILOT_POST_CROSS_GEOMETRY"
PROFILE_PRE_GEOMETRY = "PAPER_OR_UNPILOTED_PRE_GEOMETRY"
TRAINING_PROFILE = PROFILE_LIVE_PILOT
EFFECTIVE_EXECUTION_PARITY = "INCOMPLETE"
EFFECTIVE_EXECUTION_PARITY_MISSING = (
    "legacy_pretrade_score (scoring.calculate) not replayed",
    "historical order flow / order book / open interest unavailable",
    "market_risk_runtime (macro / news feeds) not replayable",
    "pilot_guard_operational and pre-dispatch spread/depth are live-only",
)


@dataclass(frozen=True)
class HookObservation:
    """Exactly the values TradingEngine._ai_gate passes to the AI."""
    symbol: str
    direction: str
    decision_ts: int
    entry: float
    stop: float
    rr: float
    strategy_score: float
    nexus_confidence: float
    cost_fraction: float
    profile: str
    population: str = POPULATION

    def to_dict(self):
        return asdict(self)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()


def engine_profile(engine) -> str:
    """Which pre-hook path the running engine executes (see module doc)."""
    live_pilot = (not bool(getattr(engine, "paper_trade", True))
                  and bool(getattr(getattr(engine, "pilot", None), "enabled", False)))
    return PROFILE_LIVE_PILOT if live_pilot else PROFILE_PRE_GEOMETRY


def from_engine(sig, nx_dec, *, decision_ts: int, cost_fraction: float, profile: str) -> HookObservation:
    """The observation at the real call site: sig and nx_dec as the engine holds them."""
    return HookObservation(
        symbol=str(sig.symbol), direction=str(sig.direction).upper(), decision_ts=int(decision_ts),
        entry=float(sig.entry), stop=float(sig.sl), rr=float(sig.rr),
        strategy_score=float(getattr(sig, "score", 0) or 0),
        nexus_confidence=float(getattr(nx_dec, "confidence", 0.0) or 0.0),
        cost_fraction=float(cost_fraction), profile=profile)


def _approved(nx) -> bool:
    return getattr(nx, "execution_allowed", False) is True


def evaluate_candidate(sig, *, decision_ts: int, funnel: dict, min_entry_score: float, nx_initial,
                       geometry: dict | None, nx_final, cost_fraction: float,
                       profile: str = TRAINING_PROFILE) -> dict:
    """Replay / shadow-observer reconstruction of the hook for ONE candidate.

    ``funnel`` = nexus_oos_execution_parity.funnel_flags(sig, ...);
    ``geometry`` = production_geometry(...) (LIVE_PILOT profile only);
    ``nx_final`` = the NEXUS re-run on the ADJUSTED geometry (None otherwise).
    Returns {"eligible", "stage", "observation"} where stage names the first
    gate that stopped the candidate (or "AI_HOOK")."""
    def out(stage, obs=None):
        return {"eligible": obs is not None, "stage": stage, "observation": obs}
    if funnel["adjusted_score"] < float(min_entry_score):
        return out("SCAN_SESSION_ADJUSTED_SCORE")
    if not funnel["regime_allows_direction"]:
        return out("SCAN_REGIME_DIRECTION")
    if not funnel["expected_pnl_positive"]:
        return out("SCAN_EXPECTED_PNL")
    if not _approved(nx_initial):
        return out("NEXUS_REJECTED")
    stop, rr, nx = float(sig.sl), float(sig.rr), nx_initial
    if profile == PROFILE_LIVE_PILOT:
        status = (geometry or {}).get("status")
        if status not in ("SAFE", "ADJUSTED"):
            return out("CROSS_GEOMETRY_BLOCK")
        if status == "ADJUSTED":
            if not _approved(nx_final):
                return out("NEXUS_RECHECK_REJECTED")
            stop = round(float(geometry["sl"]), 8)
            rr = round(float(geometry["rr"]), 2)
            nx = nx_final
    elif profile != PROFILE_PRE_GEOMETRY:
        raise ValueError(f"unknown hook profile {profile}")
    obs = HookObservation(
        symbol=str(sig.symbol), direction=str(sig.direction).upper(), decision_ts=int(decision_ts),
        entry=float(sig.entry), stop=stop, rr=rr, strategy_score=float(funnel["adjusted_score"]),
        nexus_confidence=float(getattr(nx, "confidence", 0.0) or 0.0),
        cost_fraction=float(cost_fraction), profile=profile)
    return out("AI_HOOK", obs)


def features(obs: HookObservation, k15, k1h, k4h):
    """Canonical features for a hook observation (replay, engine, observer)."""
    from bot.ai import features as fx
    return fx.candidate_features(k15, k1h, k4h, decision_ts=obs.decision_ts, direction=obs.direction,
                                 strategy_score=obs.strategy_score, entry=obs.entry, stop=obs.stop,
                                 rr=obs.rr, cost_fraction=obs.cost_fraction,
                                 nexus_confidence=obs.nexus_confidence)
