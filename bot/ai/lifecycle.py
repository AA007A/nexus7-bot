"""AI model lifecycle. Promotion is one step at a time and needs the named
evidence; there is no path that skips a state and no automatic promotion to
LIVE_CHAMPION. This phase can create at most a SHADOW_CHALLENGER.

  RESEARCH_CANDIDATE -> SHADOW_CHALLENGER : AI_RESEARCH_PROMOTION_GATE == PASS and
                                            MODEL_SELECTION_STABLE
  SHADOW_CHALLENGER  -> PAPER_CHALLENGER  : FORWARD_SHADOW_EVIDENCE == PASS
  PAPER_CHALLENGER   -> LIVE_CHAMPION     : FORWARD_PAPER_EVIDENCE == PASS and Stage-C
                                            (code + AI identity) PASS and human approval
"""
from __future__ import annotations

STATES = ("RESEARCH_CANDIDATE", "SHADOW_CHALLENGER", "PAPER_CHALLENGER", "LIVE_CHAMPION")
REQUIREMENTS = {
    ("RESEARCH_CANDIDATE", "SHADOW_CHALLENGER"): ("AI_RESEARCH_PROMOTION_GATE", "MODEL_SELECTION_STABLE"),
    ("SHADOW_CHALLENGER", "PAPER_CHALLENGER"): ("FORWARD_SHADOW_EVIDENCE",),
    ("PAPER_CHALLENGER", "LIVE_CHAMPION"): ("FORWARD_PAPER_EVIDENCE", "STAGE_C_CODE", "STAGE_C_AI_IDENTITY",
                                            "HUMAN_APPROVAL"),
}
# Execution modes each state may run in (LIVE only for a LIVE_CHAMPION).
ALLOWED_MODES = {"RESEARCH_CANDIDATE": ("SHADOW",), "SHADOW_CHALLENGER": ("SHADOW",),
                 "PAPER_CHALLENGER": ("SHADOW", "PAPER"), "LIVE_CHAMPION": ("SHADOW", "PAPER", "LIVE")}


class PromotionRefused(PermissionError):
    pass


def promote(state: str, target: str, evidence: dict) -> str:
    req = REQUIREMENTS.get((state, target))
    if req is None:
        raise PromotionRefused(f"no lifecycle path {state}->{target}")
    missing = [k for k in req if (evidence or {}).get(k) not in ("PASS", True)]
    if missing:
        raise PromotionRefused(f"{state}->{target} missing evidence: {missing}")
    return target


def mode_allowed(state: str, mode: str) -> bool:
    return mode in ALLOWED_MODES.get(state, ())
