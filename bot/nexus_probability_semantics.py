"""Declared semantics of ``nexus_probability.heuristic_win_probability``.

``nexus_ai.py`` and ``nexus_probability.py`` are byte-pinned to the production
baseline (tests/test_release_source_regression.py), so their semantics are
declared here instead of by editing them.

``p = min(0.75, 0.30 + 0.45 * confidence / 100)`` is a hand-chosen monotone
transform of ensemble confidence. It has never been fitted to outcomes and is
NOT an empirical win probability. Operator-facing text labels the derived EV
as heuristic ("EV heur. ... p não calibrado").

Execution role: the derived EV can only VETO (EV <= 0 after costs rejects a
candidate). Approval always also requires ``score >= threshold``. The veto's
incremental value is measured OOS by the ``no_ev_gate`` ablation, and
calibration against a base-rate predictor by
``nexus_oos_research.calibration_report``. Until calibration beats the base
rate out of sample, this remains a heuristic.
"""
from bot.nexus_probability import PROBABILITY_MODEL, heuristic_win_probability

PROBABILITY_KIND = "HEURISTIC_SCORE_TRANSFORM_UNCALIBRATED"
EMPIRICALLY_CALIBRATED = False
EXECUTION_ROLE = "VETO_ONLY_NEVER_APPROVES"

__all__ = [
    "PROBABILITY_MODEL", "PROBABILITY_KIND", "EMPIRICALLY_CALIBRATED",
    "EXECUTION_ROLE", "heuristic_win_probability",
]
