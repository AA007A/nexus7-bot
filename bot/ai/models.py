"""Auditable meta-models with SAFE serialization (canonical JSON + sha256).

Model kinds (no opaque networks):
  LOGISTIC_L2        regularized logistic regression (IRLS), Model A candidate
  BOOSTED_STUMPS     gradient-boosted depth-1 trees on quantile thresholds
                     (logistic loss for Model A, squared loss for Model B)
  RIDGE              ridge regression, Model B candidate
  NEXUS_HEURISTIC    the existing rule/heuristic ensemble confidence, baseline only
                     (NOT a probability; never presented as one)

Artifacts are plain JSON. Loading never unpickles or executes code; a model
whose sha256 differs from the expected/pinned hash is refused.
"""
from __future__ import annotations

import hashlib
import json
import math

import numpy as np

ARTIFACT_SCHEMA = "NEXUS7_AI_MODEL_V1"


class ModelIntegrityError(ValueError):
    pass


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


class Standardizer:
    def __init__(self, mean=None, scale=None):
        self.mean, self.scale = mean, scale

    def fit(self, X):
        self.mean = X.mean(axis=0)
        sd = X.std(axis=0)
        self.scale = np.where(sd > 1e-12, sd, 1.0)
        return self

    def transform(self, X):
        return (X - self.mean) / self.scale

    def to_json(self):
        return {"mean": [float(x) for x in self.mean], "scale": [float(x) for x in self.scale]}

    @classmethod
    def from_json(cls, d):
        return cls(np.array(d["mean"], float), np.array(d["scale"], float))


class LogisticL2:
    kind = "LOGISTIC_L2"

    def __init__(self, l2=1.0, iters=50):
        self.l2, self.iters = float(l2), int(iters)
        self.std, self.w, self.b = None, None, 0.0

    def fit(self, X, y, sample_weight=None):
        self.std = Standardizer().fit(X)
        Z = self.std.transform(X)
        n, d = Z.shape
        A = np.hstack([Z, np.ones((n, 1))])
        w = np.zeros(d + 1)
        sw = np.ones(n) if sample_weight is None else np.asarray(sample_weight, float)
        reg = np.full(d + 1, self.l2)
        reg[-1] = 0.0
        for _ in range(self.iters):
            p = _sigmoid(A @ w)
            g = A.T @ (sw * (p - y)) + reg * w
            H = (A * (sw * p * (1 - p))[:, None]).T @ A + np.diag(reg + 1e-9)
            step = np.linalg.solve(H, g)
            w -= step
            if np.max(np.abs(step)) < 1e-8:
                break
        self.w, self.b = w[:-1], float(w[-1])
        return self

    def decision_function(self, X):
        return self.std.transform(X) @ self.w + self.b

    def predict_proba(self, X):
        return _sigmoid(self.decision_function(X))

    def params(self):
        return {"l2": self.l2, "iters": self.iters, "std": self.std.to_json(),
                "w": [float(x) for x in self.w], "b": self.b}

    @classmethod
    def from_params(cls, p):
        m = cls(p["l2"], p["iters"])
        m.std = Standardizer.from_json(p["std"])
        m.w, m.b = np.array(p["w"], float), float(p["b"])
        return m


class Ridge:
    kind = "RIDGE"

    def __init__(self, l2=10.0):
        self.l2 = float(l2)
        self.std, self.w, self.b = None, None, 0.0

    def fit(self, X, y, sample_weight=None):
        self.std = Standardizer().fit(X)
        Z = self.std.transform(X)
        self.b = float(np.mean(y))
        d = Z.shape[1]
        self.w = np.linalg.solve(Z.T @ Z + self.l2 * np.eye(d), Z.T @ (y - self.b))
        return self

    def predict(self, X):
        return self.std.transform(X) @ self.w + self.b

    def params(self):
        return {"l2": self.l2, "std": self.std.to_json(), "w": [float(x) for x in self.w], "b": self.b}

    @classmethod
    def from_params(cls, p):
        m = cls(p["l2"])
        m.std = Standardizer.from_json(p["std"])
        m.w, m.b = np.array(p["w"], float), float(p["b"])
        return m


class BoostedStumps:
    """Gradient boosting with depth-1 trees. loss: 'logistic' | 'squared'."""
    kind = "BOOSTED_STUMPS"

    def __init__(self, loss="logistic", n_estimators=100, learning_rate=0.1, n_bins=16,
                 min_leaf=50):
        self.loss, self.n_estimators = loss, int(n_estimators)
        self.learning_rate, self.n_bins, self.min_leaf = float(learning_rate), int(n_bins), int(min_leaf)
        self.base, self.stumps = 0.0, []

    def fit(self, X, y, sample_weight=None):
        n, d = X.shape
        thresholds = []
        for j in range(d):
            qs = np.unique(np.quantile(X[:, j], np.linspace(0, 1, self.n_bins + 1)[1:-1]))
            thresholds.append(qs)
        if self.loss == "logistic":
            p0 = float(np.clip(np.mean(y), 1e-4, 1 - 1e-4))
            self.base = math.log(p0 / (1 - p0))
        else:
            self.base = float(np.mean(y))
        f = np.full(n, self.base)
        self.stumps = []
        for _ in range(self.n_estimators):
            resid = (y - _sigmoid(f)) if self.loss == "logistic" else (y - f)
            best = None
            for j in range(d):
                col = X[:, j]
                for t in thresholds[j]:
                    left = col <= t
                    nl = int(left.sum())
                    if nl < self.min_leaf or n - nl < self.min_leaf:
                        continue
                    sl, sr = resid[left].sum(), resid[~left].sum()
                    gain = sl * sl / nl + sr * sr / (n - nl)
                    if best is None or gain > best[0]:
                        best = (gain, j, float(t), sl / nl, sr / (n - nl))
            if best is None:
                break
            _, j, t, vl, vr = best
            vl, vr = self.learning_rate * vl, self.learning_rate * vr
            if self.loss == "logistic":
                vl, vr = 4 * vl, 4 * vr          # Newton step for logistic ~ /p(1-p) <= 4
            self.stumps.append((j, t, vl, vr))
            f += np.where(X[:, j] <= t, vl, vr)
        return self

    def raw(self, X):
        f = np.full(X.shape[0], self.base)
        for j, t, vl, vr in self.stumps:
            f += np.where(X[:, j] <= t, vl, vr)
        return f

    def predict_proba(self, X):
        return _sigmoid(self.raw(X))

    def predict(self, X):
        return self.raw(X)

    def params(self):
        return {"loss": self.loss, "n_estimators": self.n_estimators,
                "learning_rate": self.learning_rate, "n_bins": self.n_bins, "min_leaf": self.min_leaf,
                "base": self.base, "stumps": [[int(j), float(t), float(a), float(b)]
                                              for j, t, a, b in self.stumps]}

    @classmethod
    def from_params(cls, p):
        m = cls(p["loss"], p["n_estimators"], p["learning_rate"], p["n_bins"], p["min_leaf"])
        m.base = float(p["base"])
        m.stumps = [tuple(s) for s in p["stumps"]]
        return m


class NexusHeuristic:
    """Baseline: the existing ensemble confidence (0-100) as a SCORE. It is a
    heuristic, not an empirical probability; only calibration can make it one."""
    kind = "NEXUS_HEURISTIC"

    def __init__(self, column: int):
        self.column = int(column)

    def fit(self, X, y, sample_weight=None):
        return self

    def predict_proba(self, X):
        return np.clip(X[:, self.column] / 100.0, 0.0, 1.0)

    def params(self):
        return {"column": self.column}

    @classmethod
    def from_params(cls, p):
        return cls(p["column"])


KINDS = {c.kind: c for c in (LogisticL2, Ridge, BoostedStumps, NexusHeuristic)}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def artifact(model, *, role: str, feature_names, feature_schema_sha256: str, hyperparameters: dict,
             training_manifest: dict, training_code_sha: str | None, created_at: str,
             training_period: dict, calibration: dict | None = None) -> dict:
    body = {"schema": ARTIFACT_SCHEMA, "role": role, "kind": model.kind, "params": model.params(),
            "feature_names": list(feature_names), "feature_schema_sha256": feature_schema_sha256,
            "hyperparameters": hyperparameters, "training_manifest": training_manifest,
            "training_code_sha": training_code_sha, "created_at": created_at,
            "training_period": training_period, "calibration": calibration}
    return {**body, "sha256": hashlib.sha256(canonical(body)).hexdigest()}


def artifact_sha(art: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in art.items() if k != "sha256"})).hexdigest()


def load_artifact(raw, *, expected_sha256: str | None, expected_feature_schema: str):
    """Safe load: JSON only, hash verified, schema pinned. Never unpickles."""
    if isinstance(raw, (bytes, bytearray)):
        if raw[:1] == b"\x80" or raw[:2] in (b"\x80\x02", b"\x80\x03", b"\x80\x04", b"\x80\x05"):
            raise ModelIntegrityError("pickle payloads are refused")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ModelIntegrityError("artifact is not UTF-8 JSON") from exc
    try:
        art = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ModelIntegrityError("artifact is not JSON") from exc
    if not isinstance(art, dict) or art.get("schema") != ARTIFACT_SCHEMA:
        raise ModelIntegrityError("unknown artifact schema")
    actual = artifact_sha(art)
    if art.get("sha256") != actual:
        raise ModelIntegrityError("artifact content does not match its sha256")
    if expected_sha256 is None or actual != expected_sha256:
        raise ModelIntegrityError("MODEL_HASH_MISMATCH")
    if art.get("feature_schema_sha256") != expected_feature_schema:
        raise ModelIntegrityError("FEATURE_SCHEMA_MISMATCH")
    cls = KINDS.get(art.get("kind"))
    if cls is None:
        raise ModelIntegrityError("unknown model kind")
    return cls.from_params(art["params"]), art
