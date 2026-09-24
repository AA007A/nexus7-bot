"""AIDecisionAuthority: the AI decides opportunity quality — TRADE or ABSTAIN.

It outputs a typed ``AIDecision``. ABSTAIN is a first-class result. The AI
never sizes, never overrides risk, and cannot enable itself (see
authority_chain, halt). Every abstention carries explicit reason codes.

Identity: every decision is bound to the exact AI_MODEL_BUNDLE_V1 (whose
sha256 covers classifier, regressor, calibration, decision policy, feature
schema, AI version, training code / dataset / period and selection evidence),
the decision policy sha256, the feature schema sha256 and the feature hash.
Changing ANY of them changes decision_id and therefore client_oid.

Canonical cost contract (one equation, training == runtime):
    predicted_net_r = predicted_gross_r
                      - fees_r                              (entry + exit taker fees)
                      - CONSERVATIVE_SLIPPAGE_BUFFER_r      (expected slippage charged AGAIN:
                                                             gross already contains adverse
                                                             fills; this is a deliberate buffer)
                      - funding_r
                      - decision_uncertainty_buffer_r
Historical labels: modeled_net_r (production-parity r) = gross_market_r
(entry/exit slippage inside fills) - fees_r + funding_r.
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

AI_VERSION = "NEXUS7_AI_DECISION_AUTHORITY_V2"
POLICY_VERSION = "NEXUS7_AI_DECISION_POLICY_V2"
BUNDLE_SCHEMA = "AI_MODEL_BUNDLE_V1"

LONG, SHORT, HOLD, ABSTAIN = "LONG", "SHORT", "HOLD", "ABSTAIN"
TRADABLE_REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "LOW_VOLATILITY",
                    "BREAKOUT", "CHOP")


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class DecisionPolicy:
    """Frozen thresholds. Numerical values come from TRAINING/VALIDATION only
    (training.select_policy) and are frozen before any test evaluation."""
    min_p_profitable: float
    min_expected_net_r: float
    uncertainty_buffer_r: float = 0.05
    allowed_directions: tuple = ()                # nothing enabled unless validation proves it
    supported_regimes: tuple = ()
    max_data_age_ms: int = 2 * 15 * 60_000
    max_decision_latency_ms: float = 2_000.0
    probability_authorizes: bool = True           # False when calibration failed on TEST
    version: str = POLICY_VERSION

    def to_json(self):
        return {**asdict(self), "allowed_directions": list(self.allowed_directions),
                "supported_regimes": list(self.supported_regimes)}

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canon(self.to_json())).hexdigest()

    @property
    def abstain_all(self) -> bool:
        return not self.allowed_directions

    @classmethod
    def from_json(cls, d):
        d = dict(d)
        d["allowed_directions"] = tuple(d.get("allowed_directions") or ())
        d["supported_regimes"] = tuple(d.get("supported_regimes") or ())
        return cls(**d)


ABSTAIN_ALL = DecisionPolicy(1.01, 1e9, allowed_directions=(), supported_regimes=())


def cost_contract(*, predicted_gross_r: float, fees_r: float, conservative_slippage_buffer_r: float,
                  funding_r: float, decision_uncertainty_buffer_r: float) -> dict:
    """The ONE cost equation (training and runtime)."""
    net = (float(predicted_gross_r) - abs(fees_r) - abs(conservative_slippage_buffer_r)
           - abs(funding_r) - abs(decision_uncertainty_buffer_r))
    return {"predicted_gross_r": float(predicted_gross_r), "fees_r": abs(fees_r),
            "CONSERVATIVE_SLIPPAGE_BUFFER_r": abs(conservative_slippage_buffer_r),
            "funding_r": abs(funding_r),
            "decision_uncertainty_buffer_r": abs(decision_uncertainty_buffer_r),
            "predicted_net_r": net}


@dataclass
class AIDecision:
    decision_id: str
    timestamp: int
    symbol: str
    side: str
    direction_proposed: str
    ai_score: float | None
    confidence: float | None
    probability_raw: float | None
    probability_calibrated: float | None
    expected_r: float | None
    expected_net_r_after_costs: float | None
    cost_breakdown: dict | None
    regime: str
    model_version: str | None
    model_sha256: str | None
    policy_sha256: str | None
    feature_version: str
    feature_schema_sha256: str
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


def bundle_manifest(*, classifier_artifact: dict, regressor_artifact: dict, calibration: dict,
                    policy: DecisionPolicy, training_code_sha: str | None, dataset_manifest_sha256: str | None,
                    training_period: dict, selection_evidence: dict, created_at: str,
                    lifecycle_state: str = "RESEARCH_CANDIDATE", hook_population: str | None = None,
                    hook_profile: str | None = None) -> dict:
    from bot.ai import hook as ai_hook
    body = {"schema": BUNDLE_SCHEMA, "ai_version": AI_VERSION,
            "classifier_sha256": classifier_artifact["sha256"],
            "regressor_sha256": regressor_artifact["sha256"],
            "calibration": calibration, "decision_policy": policy.to_json(),
            "decision_policy_sha256": policy.sha256,
            "feature_schema_version": fx.FEATURE_SCHEMA_VERSION,
            "feature_schema_sha256": fx.schema_hash(),
            "training_code_sha": training_code_sha,
            "training_dataset_manifest_sha256": dataset_manifest_sha256,
            "training_period": training_period, "selection_evidence": selection_evidence,
            "created_at": created_at, "lifecycle_state": lifecycle_state,
            "hook_population": hook_population or ai_hook.POPULATION,
            "hook_profile": hook_profile or ai_hook.TRAINING_PROFILE}
    return {**body, "bundle_sha256": hashlib.sha256(_canon(body)).hexdigest()}


def bundle_sha(manifest: dict) -> str:
    return hashlib.sha256(_canon({k: v for k, v in manifest.items() if k != "bundle_sha256"})).hexdigest()


@dataclass
class ModelBundle:
    """AI_MODEL_BUNDLE_V1: every decision-affecting component, one identity."""
    classifier: object
    regressor: object
    calibrator: object
    policy: DecisionPolicy
    manifest: dict

    @property
    def sha256(self) -> str:
        return self.manifest["bundle_sha256"]

    @property
    def version(self) -> str:
        return self.manifest["bundle_sha256"][:16]

    @classmethod
    def load(cls, manifest_raw, classifier_raw, regressor_raw, *, pinned_bundle_sha: str | None):
        """Startup validation: bundle hash, component hashes, calibration,
        policy hash, feature schema and AI version. Any mismatch raises."""
        man = json.loads(manifest_raw) if isinstance(manifest_raw, (str, bytes)) else dict(manifest_raw)
        if man.get("schema") != BUNDLE_SCHEMA:
            raise mdl.ModelIntegrityError("unknown bundle schema")
        if bundle_sha(man) != man.get("bundle_sha256"):
            raise mdl.ModelIntegrityError("bundle content does not match its sha256")
        if pinned_bundle_sha is None or man["bundle_sha256"] != pinned_bundle_sha:
            raise mdl.ModelIntegrityError("MODEL_HASH_MISMATCH")
        if man.get("feature_schema_sha256") != fx.schema_hash():
            raise mdl.ModelIntegrityError("FEATURE_SCHEMA_MISMATCH")
        if man.get("ai_version") != AI_VERSION:
            raise mdl.ModelIntegrityError("AI_VERSION_MISMATCH")
        from bot.ai import hook as ai_hook
        if man.get("hook_population") != ai_hook.POPULATION or man.get("hook_profile") not in (
                ai_hook.PROFILE_LIVE_PILOT, ai_hook.PROFILE_PRE_GEOMETRY):
            raise mdl.ModelIntegrityError("HOOK_POPULATION_MISMATCH")
        policy = DecisionPolicy.from_json(man["decision_policy"])
        if policy.sha256 != man.get("decision_policy_sha256"):
            raise mdl.ModelIntegrityError("POLICY_HASH_MISMATCH")
        schema = fx.schema_hash()
        c, _ = mdl.load_artifact(classifier_raw, expected_sha256=man["classifier_sha256"],
                                 expected_feature_schema=schema)
        r, _ = mdl.load_artifact(regressor_raw, expected_sha256=man["regressor_sha256"],
                                 expected_feature_schema=schema)
        return cls(c, r, cal.from_json(man.get("calibration")), policy, man)


def decision_id(*, symbol: str, ts: int, direction: str, feature_hash: str | None,
                feature_schema_sha256: str, bundle_sha256: str | None, policy_sha256: str | None,
                candidate_sha: str | None) -> str:
    """Deterministic: the same observation under the same bundle, policy,
    schema and code identity always yields the same id (so a duplicated
    decision can never become a duplicated order); change any one -> new id."""
    body = "|".join(str(x) for x in (symbol, int(ts), direction, feature_hash, feature_schema_sha256,
                                      bundle_sha256, policy_sha256, candidate_sha))
    return str(uuid.UUID(hashlib.sha256(body.encode()).hexdigest()[:32]))


class AIDecisionAuthority:
    def __init__(self, bundle: ModelBundle | None, *, pinned_bundle_sha: str | None,
                 candidate_sha: str | None = None, clock=time.perf_counter):
        self.bundle = bundle
        self.policy = bundle.policy if bundle is not None else ABSTAIN_ALL
        self.pinned_bundle_sha, self.candidate_sha = pinned_bundle_sha, candidate_sha
        self.clock = clock

    def decide(self, *, symbol: str, direction: str, features: fx.FeatureVector, regime: str,
               fees_r: float, slippage_r: float, funding_r: float = 0.0,
               now_ms: int | None = None) -> AIDecision:
        t0 = self.clock()
        direction = str(direction).upper()
        pol = self.policy
        vetoes, reasons = [], []
        now_ms = features.decision_ts if now_ms is None else int(now_ms)
        age = now_ms - features.newest_candle_close_ts
        bsha = self.bundle.sha256 if self.bundle is not None else None

        if self.bundle is None:
            vetoes.append("MODEL_UNAVAILABLE")
        elif self.pinned_bundle_sha is None or bsha != self.pinned_bundle_sha:
            vetoes.append("MODEL_HASH_MISMATCH")
        if features.schema_sha256 != fx.schema_hash():
            vetoes.append("FEATURE_SCHEMA_MISMATCH")
        if age > pol.max_data_age_ms or age < 0:
            vetoes.append("STALE_DATA")
        if features.abstain_required:
            vetoes.append("REQUIRED_FEATURE_MISSING")
        if pol.abstain_all:
            vetoes.append("POLICY_ABSTAIN_ALL")
        if regime in rg.NEVER_TRADABLE or regime not in pol.supported_regimes:
            vetoes.append("UNSUPPORTED_REGIME")
        if direction not in pol.allowed_directions:
            vetoes.append(f"DIRECTION_NOT_ENABLED_{direction}")
        if not pol.probability_authorizes:
            vetoes.append("CALIBRATION_NOT_AUTHORIZED")

        p_raw = p_cal = exp_r = net_r = None
        costs = None
        if self.bundle is not None and "MODEL_HASH_MISMATCH" not in vetoes \
                and "FEATURE_SCHEMA_MISMATCH" not in vetoes:
            X = np.array([features.model_input()], float)
            p_raw = float(self.bundle.classifier.predict_proba(X)[0])
            p_cal = float(self.bundle.calibrator.transform(np.array([p_raw]))[0])
            exp_r = float(self.bundle.regressor.predict(X)[0])            # predicted GROSS R
            costs = cost_contract(predicted_gross_r=exp_r, fees_r=fees_r,
                                  conservative_slippage_buffer_r=slippage_r, funding_r=funding_r,
                                  decision_uncertainty_buffer_r=pol.uncertainty_buffer_r)
            net_r = costs["predicted_net_r"]
        if not vetoes:
            if p_cal is None or p_cal < pol.min_p_profitable:
                vetoes.append("PROBABILITY_BELOW_MIN")
            if net_r is None or net_r <= pol.min_expected_net_r:
                vetoes.append("NET_EDGE_BELOW_MIN")
        latency_ms = (self.clock() - t0) * 1000.0
        if latency_ms > pol.max_decision_latency_ms:
            vetoes.append("DECISION_LATENCY_EXCEEDED")
        side = direction if not vetoes else ABSTAIN
        reasons.append("APPROVED_NET_EDGE" if not vetoes else "ABSTAIN")
        fh = features.feature_hash()
        psha = pol.sha256
        return AIDecision(
            decision_id=decision_id(symbol=symbol, ts=features.decision_ts, direction=direction,
                                    feature_hash=fh, feature_schema_sha256=features.schema_sha256,
                                    bundle_sha256=bsha, policy_sha256=psha,
                                    candidate_sha=self.candidate_sha),
            timestamp=features.decision_ts, symbol=symbol, side=side, direction_proposed=direction,
            ai_score=(None if p_cal is None else round(100 * p_cal, 4)), confidence=p_cal,
            probability_raw=p_raw, probability_calibrated=p_cal, expected_r=exp_r,
            expected_net_r_after_costs=net_r, cost_breakdown=costs, regime=regime,
            model_version=(self.bundle.version if self.bundle else None), model_sha256=bsha,
            policy_sha256=psha, feature_version=features.schema_version,
            feature_schema_sha256=features.schema_sha256, feature_hash=fh, reason_codes=reasons,
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
