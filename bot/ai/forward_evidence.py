"""Predeclared FORWARD evidence contracts (fixed BEFORE any forward data exists).

Forward evidence is the only evidence that can move a SHADOW_CHALLENGER to
PAPER_CHALLENGER (SHADOW contract) or a PAPER_CHALLENGER toward LIVE_CHAMPION
(PAPER contract). Historical OOS was previously inspected and can never do it.
Changing any number below changes CONTRACT_SHA256 and restarts the clock.

Decisions are journaled durably (bot.ai.runtime / database.ai_decisions) under
one bundle sha256; a bundle change restarts accumulation. Statistics use the
frozen methodology (horizon-aware block bootstrap, influence-score residual ACF).
"""
from __future__ import annotations

import hashlib
import json

SHADOW_CONTRACT_V1 = {  # superseded before any forward data existed (never started)
    "name": "FORWARD_SHADOW_EVIDENCE_V1",
    "mode": "SHADOW",
    "process": "ISOLATED_SHADOW_OBSERVER (bot.ai.shadow_observer; deploy/ai_shadow_observer); zero exchange "
               "mutation capability; no exchange credentials; not the production engine",
    "hook_population": "AI_RUNTIME_HOOK_POPULATION_V1",
    "hook_profile": "LIVE_PILOT_POST_CROSS_GEOMETRY",
    "min_calendar_days": 30,
    "min_ai_decisions": 300,
    "min_ai_approved_resolved": 60,
    "min_symbols_with_approved": 3,
    "identity": "single bundle_sha256 + policy_sha256 + feature_schema_sha256 + code sha for the whole window",
    "outcome": "production-parity modeled net R of each AI-approved candidate (same simulator as replay)",
    "pass_requires": [
        "AI-approved mean net R > 0 AND block-bootstrap CI lower bound > 0 (authority VALID)",
        "uplift vs all executable candidates > 0 AND paired CI lower bound > 0",
        "calibrated probability beats base-rate Brier on the forward window",
        "cost stress FEES_PLUS_50PCT / SLIPPAGE_X2 / COMBINED all > 0",
        "no single symbol > 50% and no single week > 50% of positive R",
        "zero decision/runtime feature parity violations; zero journal gaps; zero MODEL/FEATURE mismatches",
    ],
    "fail_closed": ["any HALT not operator-cleared with reason", "journal hash/row missing",
                    "bundle change mid-window (restart the window)"],
}
PAPER_CONTRACT = {
    "name": "FORWARD_PAPER_EVIDENCE_V1",
    "mode": "PAPER",
    "hook_profile_requirement": "LIVE_PILOT_POST_CROSS_GEOMETRY. A PAPER_TRADE engine runs the pre-geometry "
                                "hook profile, so PAPER evidence cannot transfer to LIVE until a paper path "
                                "reproduces the LIVE pilot hook (runtime halts on a profile mismatch)",
    "min_calendar_days": 30,
    "min_paper_trades": 60,
    "min_symbols_traded": 3,
    "identity": "same bundle as the passing SHADOW window",
    "outcome": "PaperAdapter fills (production fees, slippage, lot/tick, min-lot) + production exits",
    "pass_requires": [
        "paper net expectancy R > 0 AND block-bootstrap CI lower bound > 0",
        "paper portfolio ending equity > start; max drawdown <= research limit",
        "realized paper slippage/fees within 1.5x of the modeled cost contract",
        "zero duplicate client_oid; every entry protected (native TPSL confirmed) or fail-safe closed",
        "restart drill: pending client_oid reconciled before any new entry",
    ],
    "fail_closed": ["any real order sent (orders_sent != 0)", "any unprotected position",
                    "any reconciliation uncertainty not operator-cleared"],
}


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


SHADOW_UNIVERSE = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT",
                   "DOTUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT")       # pinned production universe
PROBABILITY_BUCKET_EDGES = tuple(round(0.1 * k, 1) for k in range(11))           # deciles of p_calibrated
PREDICTED_NET_R_BUCKET_EDGES = (-1e9, -1.0, -0.5, -0.25, 0.0, 0.25, 0.5, 1.0, 1e9)

SHADOW_CONTRACT = {
    "name": "FORWARD_SHADOW_EVIDENCE_V2",
    "mode": "SHADOW",
    "process": "ISOLATED_SHADOW_OBSERVER (bot.ai.shadow_observer; deploy/ai_shadow_observer); zero exchange "
               "mutation capability; no exchange credentials; not the production engine",
    "hook_population": "AI_RUNTIME_HOOK_POPULATION_V1",
    "hook_profile": "LIVE_PILOT_POST_CROSS_GEOMETRY",
    "symbol_universe": list(SHADOW_UNIVERSE),
    "storage": "dedicated PostgreSQL evidence DB (bot.ai.evidence_store); SQLite / production DB refused",
    "outcome_resolver": "production-parity exits (nexus_oos_real_replay._parity_outcome), closed candles only",
    "gap_rule": "MISSING_15M_CANDLE_BEFORE_EXIT => RIGHT_CENSORED_DATA_GAP; DECISION_BAR_MISSING => INVALID",
    "window_end_rule": "open candidates => RIGHT_CENSORED_DATA_END (no forced close; excluded from resolved R)",
    "baselines": {
        "HOOK_BASELINE": "ALL AI_RUNTIME_HOOK_POPULATION_V1 candidates (resolved) - the uplift baseline",
        "EFFECTIVE_EXECUTION_BASELINE": "NOT MEASURED by the observer (downstream execution parity incomplete)",
    },
    "products": {
        "FORWARD_OBSERVATION_DATASET": "every hook candidate + resolved outcome, TRADE and ABSTAIN alike; "
                                       "grows even when the frozen policy abstains; research dataset only",
        "FROZEN_POLICY_FORWARD_EVIDENCE": "only candidates the frozen policy marked TRADE; zero TRADE => "
                                          "INSUFFICIENT_EVIDENCE; never includes abstained candidates",
    },
    "min_calendar_days": 30,
    "min_hook_candidates_resolved": 300,
    "min_policy_trades_resolved": 60,
    "min_symbols_with_policy_trades": 3,
    "identity": "single code sha + bundle sha256 + policy sha256 + feature schema sha256 for the whole window",
    "pass_requires": [
        "frozen-policy mean net R > 0 AND aggregate block-bootstrap CI lower bound > 0 (authority VALID)",
        "uplift vs HOOK_BASELINE > 0 AND paired CI lower bound > 0",
        "calibrated probability beats base-rate Brier on resolved forward outcomes",
        "cost stress FEES_PLUS_50PCT / SLIPPAGE_X2 / COMBINED all > 0",
        "no single symbol > 50% and no single week > 50% of positive R",
        "zero-order assertions all true; journal verified; no evidence-continuity break",
    ],
    "diagnostics_predeclared": {
        "probability_bucket_edges": list(PROBABILITY_BUCKET_EDGES),
        "predicted_net_r_bucket_edges": list(PREDICTED_NET_R_BUCKET_EDGES),
        "segments": ["direction", "regime", "symbol"],
        "rule": "diagnostics only; a threshold chosen after seeing this window is a NEW challenger and needs a "
                "NEW later untouched forward window",
    },
    "final_authority": "PASS only from the protected evaluator / trusted release process; the candidate "
                       "service reports COLLECTING / INSUFFICIENT_EVIDENCE / BLOCK only",
    "fail_closed": ["any zero-order assertion false", "journal digest failure", "evidence continuity break",
                    "identity change mid-window (restart the window)", "required symbol missing"],
}

CONTRACT_SHA256 = {"SHADOW": _sha(SHADOW_CONTRACT), "PAPER": _sha(PAPER_CONTRACT),
                   "SHADOW_V1_SUPERSEDED": _sha(SHADOW_CONTRACT_V1)}


def status(observed: dict | None, mode: str) -> dict:
    """No forward window has run: INSUFFICIENT_EVIDENCE until it has."""
    c = SHADOW_CONTRACT if mode == "SHADOW" else PAPER_CONTRACT
    if not observed:
        return {"contract": c["name"], "contract_sha256": CONTRACT_SHA256[mode],
                "verdict": "INSUFFICIENT_EVIDENCE", "reason": "no forward window observed yet"}
    return {"contract": c["name"], "contract_sha256": CONTRACT_SHA256[mode], "verdict": "BLOCK",
            "reason": "forward evaluation requires the protected evaluator (not implemented in-candidate)"}
