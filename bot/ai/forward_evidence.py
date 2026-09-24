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

SHADOW_CONTRACT = {
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


CONTRACT_SHA256 = {"SHADOW": _sha(SHADOW_CONTRACT), "PAPER": _sha(PAPER_CONTRACT)}


def status(observed: dict | None, mode: str) -> dict:
    """No forward window has run: INSUFFICIENT_EVIDENCE until it has."""
    c = SHADOW_CONTRACT if mode == "SHADOW" else PAPER_CONTRACT
    if not observed:
        return {"contract": c["name"], "contract_sha256": CONTRACT_SHA256[mode],
                "verdict": "INSUFFICIENT_EVIDENCE", "reason": "no forward window observed yet"}
    return {"contract": c["name"], "contract_sha256": CONTRACT_SHA256[mode], "verdict": "BLOCK",
            "reason": "forward evaluation requires the protected evaluator (not implemented in-candidate)"}
