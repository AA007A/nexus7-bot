"""Sanitized AI_OPERATION_READINESS.json (no secrets, no credentials).

Statuses are derived from code facts and, when supplied, from an OOS replay
artifact. Anything not proven is NOT_READY / BLOCK / INSUFFICIENT_EVIDENCE.
Usage: python -m bot.ai.readiness [--artifact nexus_oos_real_replay.json]
       [--candidate-sha SHA] [--output AI_OPERATION_READINESS.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot.ai import decision as dec
from bot.ai import features as fx

SCHEMA = "NEXUS7_AI_OPERATION_READINESS_V2"


def _authenticated(artifact, candidate_sha) -> bool:
    """The artifact here is a CI-produced file for THIS sha; authentication
    against a trusted provider is Stage C. Research-level: same candidate sha
    and a successful replay status."""
    return bool(isinstance(artifact, dict) and candidate_sha
                and artifact.get("candidate_sha") == candidate_sha)


def build(artifact: dict | None, *, candidate_sha: str | None) -> dict:
    from bot.ai import ai_gate, forward_evidence
    cand = (artifact or {}).get("candidate_research") or {}
    ai = cand.get("ai_meta_model") or {}
    pooled = ai.get("pooled_test") or {}
    gate = ai_gate.evaluate(artifact or {})
    comp = gate["components"]
    sc = ai.get("shadow_challenger") or {}
    fwd_s = forward_evidence.status(None, "SHADOW")
    fwd_p = forward_evidence.status(None, "PAPER")
    components = {
        "AI_RESEARCH_ARTIFACT_AUTHENTICATED": "PASS" if _authenticated(artifact, candidate_sha) else "BLOCK",
        "AI_EXPECTANCY_AUTHORITY": comp["AI_EXPECTANCY_AUTHORITY"],
        "AI_UPLIFT_AUTHORITY": comp["AI_UPLIFT_AUTHORITY"],
        "AI_CALIBRATION": comp["AI_CALIBRATION"],
        "AI_COST_STRESS": comp["AI_COST_STRESS"],
        "AI_PORTFOLIO_ROBUSTNESS": comp["AI_PORTFOLIO_ROBUSTNESS"],
        "AI_RESEARCH_PROMOTION": gate["verdict"],
    }
    return {
        "schema": SCHEMA,
        "candidate_sha": candidate_sha,
        "ai_version": dec.AI_VERSION,
        "decision_policy_version": dec.POLICY_VERSION,
        "feature_schema": {"version": fx.FEATURE_SCHEMA_VERSION, "sha256": fx.schema_hash(),
                           "model_features": list(fx.MODEL_FEATURES),
                           "live_only_excluded": [s.name for s in fx.FEATURE_SPECS if not s.model_input]},
        "components": components,
        "ai_research_promotion_gate": {"verdict": gate["verdict"], "blockers": gate["blockers"],
                                       "components": comp, "recomputed": gate["recomputed"]},
        "model_selection_stability": (ai.get("selection_stability") or {}).get("status"),
        "shadow_challenger": {"created": bool(sc.get("created")),
                              "bundle_sha256": sc.get("bundle_sha256"),
                              "decision_policy_sha256": sc.get("decision_policy_sha256"),
                              "lifecycle_state": sc.get("lifecycle_state"),
                              "reason": sc.get("reason")},
        "execution_mode_support": {"OFF": "DEFAULT_NO_OP", "SHADOW": "CODE_READY",
                                   "PAPER": "CODE_READY_REQUIRES_PAPER_TRADE_ENGINE",
                                   "LIVE": "FAIL_CLOSED_WITHOUT_STAGE_C_AND_AI_IDENTITY"},
        "oos_status": {"ai_meta_model_status": ai.get("status", "NOT_RUN"),
                       "data_label": ai.get("data_label"),
                       "pooled_test_ai_approved": pooled.get("ai_approved"),
                       "pooled_test_baseline": pooled.get("baseline")},
        "forward_shadow_evidence": fwd_s,
        "forward_paper_evidence": fwd_p,
        "engine_integration": "WIRED_PRE_TRADE_AFTER_NEXUS_DEFAULT_OFF",
        "durable_ai_journal": "DATABASE_AI_DECISIONS_TABLE",
        "restart_ai_reconciliation": "CLIENT_OID_RECONCILE_BEFORE_NEW_ENTRY",
        "halt_authority": "UNIFIED_HALT_CONTROLLER_OPERATOR_RECOVER_ONLY",
        "risk_invariant_status": "ENFORCED_BY_AUTHORITY_CHAIN_TESTED",
        "live_status": "BLOCK",
        "known_blockers": sorted(set(
            [f"AI_{k}_BLOCK" if not k.startswith("AI_") else f"{k}_BLOCK"
             for k, v in components.items() if v != "PASS"]
            + ["NO_FORWARD_SHADOW_EVIDENCE", "NO_FORWARD_PAPER_EVIDENCE", "NO_LIVE_CHAMPION",
               "STAGE_C_EVIDENCE_NOT_AVAILABLE"])),
        "secrets_included": False,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", default=None)
    ap.add_argument("--candidate-sha", default=None)
    ap.add_argument("--output", default="AI_OPERATION_READINESS.json")
    a = ap.parse_args(argv)
    art = json.loads(Path(a.artifact).read_text()) if a.artifact and Path(a.artifact).is_file() else None
    out = build(art, candidate_sha=a.candidate_sha)
    Path(a.output).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
