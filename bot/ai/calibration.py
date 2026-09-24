"""Probability calibration (fitted on VALIDATION data only) and metrics."""
from __future__ import annotations

import math

import numpy as np


class Platt:
    kind = "PLATT"

    def __init__(self, a=1.0, b=0.0):
        self.a, self.b = float(a), float(b)

    def fit(self, p, y):
        """Damped Newton (backtracking) on the log loss; never diverges."""
        z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
        y = np.asarray(y, float)

        def loss(a, b):
            t = a * z + b
            return float(np.sum(np.logaddexp(0, t) - y * t))

        a, b = 1.0, 0.0
        cur = loss(a, b)
        for _ in range(100):
            q = 1 / (1 + np.exp(-np.clip(a * z + b, -35, 35)))
            g = np.array([np.sum((q - y) * z), np.sum(q - y)])
            w = q * (1 - q) + 1e-9
            H = np.array([[np.sum(w * z * z) + 1e-6, np.sum(w * z)], [np.sum(w * z), np.sum(w) + 1e-6]])
            step = np.linalg.solve(H, g)
            t = 1.0
            while t > 1e-6:
                na, nb = a - t * step[0], b - t * step[1]
                new = loss(na, nb)
                if new <= cur:
                    break
                t /= 2
            if t <= 1e-6 or abs(cur - new) < 1e-10:
                break
            a, b, cur = na, nb, new
        self.a, self.b = float(a), float(b)
        return self

    def transform(self, p):
        z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
        return 1 / (1 + np.exp(-np.clip(self.a * z + self.b, -35, 35)))

    def to_json(self):
        return {"kind": self.kind, "a": self.a, "b": self.b}


class Identity:
    kind = "IDENTITY"

    def fit(self, p, y):
        return self

    def transform(self, p):
        return np.asarray(p, float)

    def to_json(self):
        return {"kind": self.kind}


def from_json(d):
    if not d or d.get("kind") == "IDENTITY":
        return Identity()
    if d.get("kind") == "PLATT":
        return Platt(d["a"], d["b"])
    raise ValueError("unknown calibrator")


def brier(p, y):
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2)) if len(y) else None


def log_loss(p, y):
    p = np.clip(np.asarray(p, float), 1e-9, 1 - 1e-9)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))) if len(y) else None


def reliability(p, y, bins=10):
    p, y = np.asarray(p, float), np.asarray(y, float)
    out, ece = [], 0.0
    edges = np.linspace(0, 1, bins + 1)
    for i in range(bins):
        m = (p >= edges[i]) & ((p < edges[i + 1]) if i < bins - 1 else (p <= edges[i + 1]))
        n = int(m.sum())
        if n:
            mp, my = float(p[m].mean()), float(y[m].mean())
            ece += n / len(p) * abs(mp - my)
            out.append({"bucket": f"{edges[i]:.1f}-{edges[i+1]:.1f}", "n": n,
                        "mean_predicted": mp, "observed_rate": my})
    return out, (float(ece) if len(p) else None)


def report(p, y) -> dict:
    y = np.asarray(y, float)
    base = float(y.mean()) if len(y) else None
    curve, ece = reliability(p, y)
    b_model, b_base = brier(p, y), brier(np.full(len(y), base if base is not None else 0.5), y)
    return {"n": int(len(y)), "base_rate": base, "brier": b_model, "brier_base_rate": b_base,
            "log_loss": log_loss(p, y),
            "log_loss_base_rate": log_loss(np.full(len(y), base if base is not None else 0.5), y),
            "ece": ece, "reliability": curve,
            "beats_base_rate": (b_model is not None and b_base is not None and b_model < b_base
                                and not math.isclose(b_model, b_base))}
