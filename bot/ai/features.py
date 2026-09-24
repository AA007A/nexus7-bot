"""Canonical AI feature pipeline — ONE implementation for RESEARCH, SHADOW,
PAPER and LIVE. No research-only feature math exists anywhere else.

Causality: every feature is computed from CLOSED candles only (candle open
time + timeframe <= decision time); a window containing an unclosed or future
candle raises ``FeatureCausalityError``. Missing values follow the declared
policy (never forward-filled from later data).

Parity: features whose historical data is not replayable (funding history is
partial, open interest and order book have no history) are declared
``LIVE_ONLY_NO_HISTORICAL_PARITY`` and are EXCLUDED from model inputs until
equivalent historical/shadow evidence exists.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

FEATURE_SCHEMA_VERSION = "NEXUS7_AI_FEATURES_V3"   # V3: canonical closed windows (V2: __missing flags)

TF_MS = {"15m": 15 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}
# Canonical feature windows: the LAST N CLOSED candles per timeframe (the
# replay's analysis windows). Runtime caches hold more history; EMA/ATR
# values depend on history length, so every path truncates to these.
FEATURE_WINDOWS = {"15m": 80, "1h": 50, "4h": 30}

CAUSAL = "CAUSAL_CLOSED_CANDLES"
CONTEXT = "CAUSAL_DECISION_CONTEXT"
LIVE_ONLY = "LIVE_ONLY_NO_HISTORICAL_PARITY"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    version: int
    source: str
    timeframe: str
    missing_policy: str          # "ZERO_WITH_FLAG" | "ABSTAIN" | "EXCLUDED"
    causality: str
    max_age_ms: int | None = None

    @property
    def model_input(self) -> bool:
        return self.causality != LIVE_ONLY


def _s(name, source, tf, missing="ZERO_WITH_FLAG", causality=CAUSAL, version=1):
    return FeatureSpec(name, version, source, tf, missing, causality,
                       2 * TF_MS[tf] if tf in TF_MS else None)


FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    _s("ret_1", "kucoin_futures_klines", "15m", "ABSTAIN"),
    _s("ret_4", "kucoin_futures_klines", "15m", "ABSTAIN"),
    _s("ret_16", "kucoin_futures_klines", "15m", "ABSTAIN"),
    _s("atr_pct_14", "kucoin_futures_klines", "15m", "ABSTAIN"),
    _s("realized_vol_32", "kucoin_futures_klines", "15m", "ABSTAIN"),
    _s("ema20_ema50_gap", "kucoin_futures_klines", "15m", "ABSTAIN"),
    _s("ema20_slope_8", "kucoin_futures_klines", "15m", "ABSTAIN"),
    _s("rsi_14", "kucoin_futures_klines", "15m", "ABSTAIN"),
    _s("adx_14", "kucoin_futures_klines", "15m"),
    _s("volume_multiple_20", "kucoin_futures_klines", "15m"),
    _s("body_ratio", "kucoin_futures_klines", "15m"),
    _s("range_position_32", "kucoin_futures_klines", "15m"),
    _s("h1_trend_slope_12", "kucoin_futures_klines", "1h"),
    _s("h4_trend_slope_6", "kucoin_futures_klines", "4h"),
    _s("h1_realized_vol_24", "kucoin_futures_klines", "1h"),
    _s("direction_long", "strategy_signal", "decision", causality=CONTEXT),
    _s("strategy_score", "strategy_signal", "decision", causality=CONTEXT),
    _s("planned_rr", "strategy_signal", "decision", causality=CONTEXT),
    _s("stop_distance_pct", "strategy_signal", "decision", causality=CONTEXT),
    _s("cost_fraction", "execution_model", "decision", causality=CONTEXT),
    _s("nexus_confidence", "nexus_heuristic_ensemble", "decision", causality=CONTEXT),
    _s("hour_sin", "clock", "decision", causality=CONTEXT),
    _s("hour_cos", "clock", "decision", causality=CONTEXT),
    _s("funding_rate", "kucoin_funding", "8h", "EXCLUDED", LIVE_ONLY),
    _s("open_interest_delta", "kucoin_oi", "1h", "EXCLUDED", LIVE_ONLY),
    _s("orderbook_imbalance", "kucoin_orderbook", "live", "EXCLUDED", LIVE_ONLY),
)
BASE_MODEL_FEATURES: tuple[str, ...] = tuple(s.name for s in FEATURE_SPECS if s.model_input)
# ZERO_WITH_FLAG: the value is imputed as 0 AND a deterministic companion
# input "<name>__missing" (1 = missing, 0 = present) is supplied to the model.
FLAGGED_FEATURES: tuple[str, ...] = tuple(s.name for s in FEATURE_SPECS
                                          if s.model_input and s.missing_policy == "ZERO_WITH_FLAG")
MODEL_FEATURES: tuple[str, ...] = BASE_MODEL_FEATURES + tuple(f"{n}__missing" for n in FLAGGED_FEATURES)


def schema_hash() -> str:
    body = [[s.name, s.version, s.source, s.timeframe, s.missing_policy, s.causality]
            for s in FEATURE_SPECS]
    return hashlib.sha256(json.dumps([FEATURE_SCHEMA_VERSION, body, list(MODEL_FEATURES),
                                      FEATURE_WINDOWS]).encode()).hexdigest()


class FeatureCausalityError(ValueError):
    pass


@dataclass
class FeatureVector:
    values: dict
    missing: tuple
    decision_ts: int
    newest_candle_close_ts: int
    schema_version: str = FEATURE_SCHEMA_VERSION
    schema_sha256: str = field(default_factory=schema_hash)

    @property
    def data_age_ms(self) -> int:
        return int(self.decision_ts - self.newest_candle_close_ts)

    @property
    def abstain_required(self) -> bool:
        spec = {s.name: s for s in FEATURE_SPECS}
        return any(spec[m].missing_policy == "ABSTAIN" for m in self.missing)

    def model_input(self) -> list[float]:
        base = [float(self.values.get(n) or 0.0) for n in BASE_MODEL_FEATURES]
        flags = [1.0 if n in self.missing else 0.0 for n in FLAGGED_FEATURES]
        return base + flags

    def feature_hash(self) -> str:
        body = {"schema": self.schema_sha256, "ts": self.decision_ts,
                "values": {k: (None if v is None else round(float(v), 12))
                           for k, v in sorted(self.values.items())}}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _ts(c: dict) -> int:
    ts = int(c.get("ts", 0) or 0)
    return ts * 1000 if ts < 100_000_000_000 else ts


def assert_closed(window, tf: str, decision_ts: int) -> int:
    """Every candle must have closed at or before the decision. Returns the
    newest close time. Raises on any unclosed / future candle."""
    newest = 0
    for c in window or ():
        close = _ts(c) + TF_MS[tf]
        if close > decision_ts:
            raise FeatureCausalityError(f"{tf} candle closes after the decision")
        newest = max(newest, close)
    return newest


def _ema(xs, n):
    k = 2.0 / (n + 1)
    out, e = [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def _rsi(c, n=14):
    if len(c) < n + 1:
        return None
    gains = [max(0.0, c[i] - c[i - 1]) for i in range(len(c) - n, len(c))]
    losses = [max(0.0, c[i - 1] - c[i]) for i in range(len(c) - n, len(c))]
    g, l_ = sum(gains) / n, sum(losses) / n
    if l_ == 0:
        return 100.0 if g > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + g / l_)


def _logret(c, k):
    return math.log(c[-1] / c[-1 - k]) if len(c) > k and c[-1 - k] > 0 and c[-1] > 0 else None


def _rvol(c, n):
    if len(c) < n + 1:
        return None
    r = [math.log(c[i] / c[i - 1]) for i in range(len(c) - n, len(c)) if c[i - 1] > 0 and c[i] > 0]
    if len(r) < 2:
        return None
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))


def _slope(c, n):
    if len(c) < n or c[-n] <= 0:
        return None
    return (c[-1] - c[-n]) / c[-n] / n


def compute_features(k15, k1h, k4h, *, decision_ts: int, direction: str, strategy_score: float,
                     entry: float, stop: float, rr: float, cost_fraction: float,
                     nexus_confidence: float | None = None) -> FeatureVector:
    """The ONLY feature implementation. Same call in research, shadow, paper, live."""
    from bot import nexus_oos_research as res
    newest = max(assert_closed(k15, "15m", decision_ts), assert_closed(k1h, "1h", decision_ts),
                 assert_closed(k4h, "4h", decision_ts))
    h = [float(k["h"]) for k in k15]
    lo = [float(k["l"]) for k in k15]
    c = [float(k["c"]) for k in k15]
    o = [float(k["o"]) for k in k15]
    v = [float(k.get("v", 0.0) or 0.0) for k in k15]
    c1 = [float(k["c"]) for k in k1h or ()]
    c4 = [float(k["c"]) for k in k4h or ()]
    e20, e50 = (_ema(c, 20), _ema(c, 50)) if len(c) >= 50 else (None, None)
    vals = {
        "ret_1": _logret(c, 1), "ret_4": _logret(c, 4), "ret_16": _logret(c, 16),
        "atr_pct_14": res.atr_pct(h, lo, c), "realized_vol_32": _rvol(c, 32),
        "ema20_ema50_gap": ((e20[-1] - e50[-1]) / c[-1]) if e20 and c[-1] > 0 else None,
        "ema20_slope_8": ((e20[-1] - e20[-9]) / c[-1]) if e20 and len(e20) > 9 and c[-1] > 0 else None,
        "rsi_14": _rsi(c), "adx_14": res.adx(h, lo, c),
        "volume_multiple_20": ((v[-1] / (sum(v[-21:-1]) / 20.0))
                               if len(v) >= 21 and sum(v[-21:-1]) > 0 else None),
        "body_ratio": ((abs(c[-1] - o[-1]) / (h[-1] - lo[-1])) if c and h[-1] > lo[-1] else None),
        "range_position_32": ((c[-1] - min(lo[-32:])) / (max(h[-32:]) - min(lo[-32:]))
                              if len(c) >= 32 and max(h[-32:]) > min(lo[-32:]) else None),
        "h1_trend_slope_12": _slope(c1, 12), "h4_trend_slope_6": _slope(c4, 6),
        "h1_realized_vol_24": _rvol(c1, 24),
        "direction_long": 1.0 if str(direction).upper() == "LONG" else 0.0,
        "strategy_score": float(strategy_score), "planned_rr": float(rr),
        "stop_distance_pct": (abs(entry - stop) / entry) if entry > 0 else None,
        "cost_fraction": float(cost_fraction),
        "nexus_confidence": None if nexus_confidence is None else float(nexus_confidence),
        "hour_sin": math.sin(2 * math.pi * ((decision_ts // 3_600_000) % 24) / 24),
        "hour_cos": math.cos(2 * math.pi * ((decision_ts // 3_600_000) % 24) / 24),
    }
    missing = tuple(n for n in BASE_MODEL_FEATURES
                    if vals.get(n) is None or not math.isfinite(float(vals[n])))
    for n in missing:
        vals[n] = None
    return FeatureVector(vals, missing, int(decision_ts), int(newest))


def closed_only(window, tf: str, decision_ts: int) -> list:
    """Drop any candle not closed at decision time (live caches include the
    forming candle). The ONLY way runtime code prepares windows."""
    return [c for c in (window or ()) if _ts(c) + TF_MS[tf] <= int(decision_ts)]


def canonical_window(window, tf: str, decision_ts: int) -> list:
    """Closed candles only, sorted, de-duplicated by open time, last N."""
    by_ts = {_ts(c): c for c in closed_only(window, tf, decision_ts)}
    return [by_ts[t] for t in sorted(by_ts)][-FEATURE_WINDOWS[tf]:]


def candidate_features(k15, k1h, k4h, *, decision_ts: int, direction: str, strategy_score: float,
                       entry: float, stop: float, rr: float, cost_fraction: float,
                       nexus_confidence: float | None):
    """Canonical entry point used by BOTH the replay and the live engine gate:
    closed windows -> FeatureVector + regime. Returns (FeatureVector, regime)."""
    from bot.ai import regime as rg
    w15, w1h, w4h = (canonical_window(k15, "15m", decision_ts), canonical_window(k1h, "1h", decision_ts),
                     canonical_window(k4h, "4h", decision_ts))
    fv = compute_features(w15, w1h, w4h, decision_ts=decision_ts, direction=direction,
                          strategy_score=strategy_score, entry=entry, stop=stop, rr=rr,
                          cost_fraction=cost_fraction, nexus_confidence=nexus_confidence)
    return fv, rg.classify(w1h)
