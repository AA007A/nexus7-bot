"""AIDecisionAuthority: the AI decides opportunity quality — TRADE or ABSTAIN.

It outputs a typed ``AIDecision``. ABSTAIN is a first-class result. The AI
never sizes, never overrides risk, and cannot enable itself (see
authority_chain, halt). Every abstention carries explicit reason codes.

Net-of-cost rule: a candidate is approvable only when
    expected_net_r = expected_gross_r - fees_r - slippage_r - funding_r - uncertainty_buffer_r
exceeds the frozen minimum edge. Gross EV alone never approves a trade.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field

import numpy as np

from bot.ai import calibration as cal
from bot.ai import features as fx
from bot.ai import models as mdl
from bot.ai import regime as rg

AI_VERSION = "NEXUS7_AI_DECISION_AUTHORITY_V1"
POLICY_VERSION = "NEXUS7_AI_DECISION_POLICY_V1"

LONG, SHORT, HOLD, ABSTAIN = "LONG", "SHORT", "HOLD", "ABSTAIN"


@dataclass(frozen=True)
class DecisionPolicy:
    """Frozen thresholds. Numerical values come from TRAINING/VALIDATION only
    (training.select_policy) and are frozen before any test evaluation."""
    min_p_profitable: float
    min_expected_net_r: float
    uncertainty_buffer_r: float = 0.05
    allowed_directions: tuple = (LONG,)          # SHORT only after its own proven edge
    supported_regimes: tuple = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY",
                                "LOW_VOLATILITY", "BREAKOUT", "CHOP")
    max_data_age_ms: int = 2 * 15 * 60_000
    max_decision_latency_ms: float = 2_000.0
    version: str = POLICY_VERSION

    def to_json(self):
        return {**asdict(self), "allowed_directions": list(self.allowed_directions),
                "supported_regimes": list(self.supported_regimes)}


@dataclass
class AIDecision:
    decision_id: str
    timestamp: int
    symbol: str
    side: str
    ai_score: float | None
    confidence: float | None
    probability_raw: float | None
    probability_calibrated: float | None
    expected_r: float | None
    expected_net_r_after_costs: float | None
    regime: str
    model_version: str | None
    model_sha256: str | None
    feature_version: str
    feature_hash: str | None
    reason_codes: list = field(default_factory=list)
    vetoes: list = field(default_factory=list)
    data_freshness_ms: int | None = None
    decision_latency_ms: float | None = None
    candidate_sha: str | None = None
    policy_version: str = POLICY_VERSION
    ai_version: str = AI_VERSION

    @property
    def is_trade(self) -> bool:
        return self.side in (LONG, SHORT) and not self.vetoes

    def to_dict(self):
        return asdict(self)


@dataclass
class ModelBundle:
    """Model A (P(profitable after costs)) + Model B (expected net R) + calibrator."""
    classifier: object
    regressor: object
    calibrator: object
    classifier_artifact: dict
    regressor_artifact: dict
    version: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256((self.classifier_artifact["sha256"] + self.regressor_artifact["sha256"]
                               ).encode()).hexdigest()

    @classmethod
    def load(cls, classifier_raw, regressor_raw, *, pinned_classifier_sha: str | None,
             pinned_regressor_sha: str | None, version: str):
        schema = fx.schema_hash()
        c, ca = mdl.load_artifact(classifier_raw, expected_sha256=pinned_classifier_sha,
                                  expected_feature_schema=schema)
        r, ra = mdl.load_artifact(regressor_raw, expected_sha256=pinned_regressor_sha,
                                  expected_feature_schema=schema)
        return cls(c, r, cal.from_json(ca.get("calibration")), ca, ra, version)


def decision_id(symbol: str, ts: int, direction: str, feature_hash: str | None, model_sha: str | None) -> str:
    """Deterministic: the same observation always yields the same decision id
    (so a duplicated decision can never become a duplicated order intent)."""
    body = f"{symbol}|{ts}|{direction}|{feature_hash}|{model_sha}"
    return str(uuid.UUID(hashlib.sha256(body.encode()).hexdigest()[:32]))


class AIDecisionAuthority:
    def __init__(self, bundle: ModelBundle | None, policy: DecisionPolicy, *,
                 pinned_bundle_sha: str | None, candidate_sha: str | None = None,
                 clock=time.perf_counter):
        self.bundle, self.policy = bundle, policy
        self.pinned_bundle_sha, self.candidate_sha = pinned_bundle_sha, candidate_sha
        self.clock = clock

    def decide(self, *, symbol: str, direction: str, features: fx.FeatureVector, regime: str,
               fees_r: float, slippage_r: float, funding_r: float = 0.0,
               now_ms: int | None = None) -> AIDecision:
        t0 = self.clock()
        direction = str(direction).upper()
        vetoes, reasons = [], []
        now_ms = features.decision_ts if now_ms is None else int(now_ms)
        age = now_ms - features.newest_candle_close_ts
        model_sha = self.bundle.sha256 if self.bundle is not None else None

        if self.bundle is None:
            vetoes.append("MODEL_UNAVAILABLE")
        elif self.pinned_bundle_sha is None or model_sha != self.pinned_bundle_sha:
            vetoes.append("MODEL_HASH_MISMATCH")
        if features.schema_sha256 != fx.schema_hash():
            vetoes.append("FEATURE_SCHEMA_MISMATCH")
        if age > self.policy.max_data_age_ms or age < 0:
            vetoes.append("STALE_DATA")
        if features.abstain_required:
            vetoes.append("REQUIRED_FEATURE_MISSING")
        if regime in rg.NEVER_TRADABLE or regime not in self.policy.supported_regimes:
            vetoes.append("UNSUPPORTED_REGIME")
        if direction not in self.policy.allowed_directions:
            vetoes.append(f"DIRECTION_NOT_ENABLED_{direction}")

        p_raw = p_cal = exp_r = net_r = None
        if not vetoes or all(v.startswith("DIRECTION_NOT_ENABLED") or v == "UNSUPPORTED_REGIME"
                             for v in vetoes):
            if self.bundle is not None and "MODEL_HASH_MISMATCH" not in vetoes:
                X = np.array([features.model_input()], float)
                p_raw = float(self.bundle.classifier.predict_proba(X)[0])
                p_cal = float(self.bundle.calibrator.transform(np.array([p_raw]))[0])
                exp_r = float(self.bundle.regressor.predict(X)[0])      # expected GROSS R
                # Same deterministic cost charge as training (training.cost_estimate_r
                # = fees_r + slippage_r here) plus funding and the uncertainty buffer.
                net_r = exp_r - abs(fees_r) - abs(slippage_r) - abs(funding_r) \
                    - self.policy.uncertainty_buffer_r
        if not vetoes:
            if p_cal is None or p_cal < self.policy.min_p_profitable:
                vetoes.append("PROBABILITY_BELOW_MIN")
            if net_r is None or net_r <= self.policy.min_expected_net_r:
                vetoes.append("NET_EDGE_BELOW_MIN")
        latency_ms = (self.clock() - t0) * 1000.0
        if latency_ms > self.policy.max_decision_latency_ms:
            vetoes.append("DECISION_LATENCY_EXCEEDED")
        side = direction if not vetoes else ABSTAIN
        reasons.append("APPROVED_NET_EDGE" if not vetoes else "ABSTAIN")
        fh = features.feature_hash()
        return AIDecision(
            decision_id=decision_id(symbol, features.decision_ts, direction, fh, model_sha),
            timestamp=features.decision_ts, symbol=symbol, side=side,
            ai_score=(None if p_cal is None else round(100 * p_cal, 4)), confidence=p_cal,
            probability_raw=p_raw, probability_calibrated=p_cal, expected_r=exp_r,
            expected_net_r_after_costs=net_r, regime=regime,
            model_version=(self.bundle.version if self.bundle else None), model_sha256=model_sha,
            feature_version=features.schema_version, feature_hash=fh, reason_codes=reasons,
            vetoes=vetoes, data_freshness_ms=int(age), decision_latency_ms=latency_ms,
            candidate_sha=self.candidate_sha)


def decision_digest(d: AIDecision) -> str:
    body = {k: v for k, v in d.to_dict().items() if k != "decision_latency_ms"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def exit_recommendation(*, symbol: str, unrealized_r: float, model_hint: float | None = None) -> dict:
    """AI position-management advice. SHADOW_ONLY: recorded, never executed.
    Position management stays deterministic until separate OOS evidence
    proves an AI exit improves outcomes."""
    action = HOLD
    if model_hint is not None and model_hint < -0.5:
        action = "EXIT"
    elif model_hint is not None and model_hint < 0:
        action = "REDUCE"
    return {"symbol": symbol, "action": action, "unrealized_r": unrealized_r,
            "mode": "SHADOW_ONLY", "executable": False}
