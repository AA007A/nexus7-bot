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
from bot.ai import execution_mode as em
from bot.ai import features as fx

SCHEMA = "NEXUS7_AI_OPERATION_READINESS_V1"


def build(artifact: dict | None, *, candidate_sha: str | None) -> dict:
    cand = (artifact or {}).get("candidate_research") or {}
    ai = cand.get("ai_meta_model") or {}
    pooled = ai.get("pooled_test") or {}
    auth = (ai.get("authority") or {})
    ai_auth = (auth.get("ai_approved_expectancy") or {}).get("authority_status")
    ai_lo = (auth.get("ai_approved_expectancy") or {}).get("authority_ci_low")
    up_lo = (auth.get("ai_uplift_vs_baseline") or {}).get("authority_ci_low")
    oos_positive = bool(ai_auth == "VALID" and ai_lo is not None and ai_lo > 0
                        and up_lo is not None and up_lo > 0)
    return {
        "schema": SCHEMA,
        "candidate_sha": candidate_sha,
        "ai_version": dec.AI_VERSION,
        "decision_policy_version": dec.POLICY_VERSION,
        "feature_schema": {"version": fx.FEATURE_SCHEMA_VERSION, "sha256": fx.schema_hash(),
                           "model_features": list(fx.MODEL_FEATURES),
                           "live_only_excluded": [s.name for s in fx.FEATURE_SPECS if not s.model_input]},
        "model": {"status": "NO_PROMOTED_MODEL", "model_sha256": None,
                  "note": "research models are retrained per replay; none is pinned for LIVE"},
        "execution_mode_support": {m: ("SUPPORTED" if m != "LIVE" else "FAIL_CLOSED_WITHOUT_STAGE_C")
                                   for m in em.MODES},
        "oos_status": {"ai_meta_model_status": ai.get("status", "NOT_RUN"),
                       "data_label": ai.get("data_label"),
                       "pooled_test_ai_approved": pooled.get("ai_approved"),
                       "pooled_test_baseline": pooled.get("baseline"),
                       "ai_authority_status": ai_auth, "ai_authority_ci_low": ai_lo,
                       "ai_uplift_authority_ci_low": up_lo,
                       "positive_edge_proven": oos_positive},
        "shadow_status": "CODE_READY_NOT_RUN_ON_LIVE_FEEDS",
        "paper_status": "CODE_READY_NOT_RUN_ON_LIVE_FEEDS",
        "risk_invariant_status": "ENFORCED_BY_AUTHORITY_CHAIN_TESTED",
        "execution_invariant_status": "IDEMPOTENT_RECONCILE_BEFORE_RETRY_TESTED",
        "live_status": "BLOCK",
        "known_blockers": [b for b in (
            None if oos_positive else "AI_EDGE_NOT_PROVEN_OOS",
            "NO_FORWARD_SHADOW_EVIDENCE", "NO_FORWARD_PAPER_EVIDENCE", "NO_PROMOTED_MODEL_ARTIFACT",
            "AI_NOT_WIRED_INTO_PRODUCTION_ENGINE", "STAGE_C_EVIDENCE_NOT_AVAILABLE",
            "REPLAY_PARITY_INCOMPLETE") if b],
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
