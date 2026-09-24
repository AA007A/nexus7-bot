"""Pinned, immutable research input for the portfolio / execution replay.

The replay must not describe itself as "production-configured" while silently
inheriting CI defaults. Every non-secret policy value it uses comes from one
committed manifest (``research/replay_policy_manifest.json``). Loading is fail
closed:

* a missing or extra key, a wrong type or an invalid risk policy raises
  ``ManifestError``;
* values that production code reads IN THIS PROCESS (analyzer / NEXUS
  thresholds, execution cost model, liquidation constants, engine class
  constants) must equal the runtime value, otherwise ``ManifestError`` is
  raised (MANIFEST_RUNTIME_MISMATCH). This guarantees the replayed strategy
  and the reported policy are the same thing.

No secrets are read. No environment variable is written.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "research" / "replay_policy_manifest.json"

_NUM = (int, float)
REQUIRED: dict[str, tuple] = {
    "LEVERAGE": _NUM, "MAX_RISK_PCT": _NUM, "MAX_MARGIN_PCT": _NUM, "MAX_DRAWDOWN": _NUM,
    "MAX_POSITIONS": (int,), "DAILY_STOP_LOSS_PCT": _NUM, "DAILY_STOP_LOSS": _NUM,
    "MIN_RR_RATIO": _NUM, "OPERATOR_MARGIN_CAP_PCT": _NUM, "MAX_STOP_STRESS_RISK_RATE": _NUM,
    "NEXUS_EXPECTED_SLIPPAGE_PCT": _NUM, "POST_TARGET_RISK": _NUM,
    "MIN_ENTRY_SCORE": (int,), "POST_TARGET_SCORE": (int,), "NEXUS_MIN_SCORE_EFFECTIVE": _NUM,
    "FEE_MULTIPLIER": _NUM, "MIN_VOLUME_MULT": _NUM,
    "DAILY_TARGET_PCT": _NUM, "DAILY_TARGET": _NUM, "DAILY_PNL_SEMANTICS": (str,),
    "TRAILING_TRIGGER": _NUM, "TRAILING_LOCK": _NUM, "PARTIAL_TP_FRACTION": _NUM,
    "TP1_FUNDING_BUFFER": _NUM, "RR_DOUBLE_MULTIPLE": _NUM,
    "ENTRY_COOLDOWN_SECONDS": (int,), "MAX_CONSEC_LOSSES": (int,), "CB_COOLDOWN_HOURS": (int,),
    "PILOT_MAX_CONCURRENT_POSITIONS": (int,), "PILOT_MIN_AVAILABLE_EQUITY_RATIO": _NUM,
    "PILOT_MAX_POSITION_MARGIN_EQUITY_RATIO": _NUM, "SINGLE_POSITION_LIQUIDATION_RULE": (bool,),
    "TAKER_FEE": _NUM, "BACKTEST_SLIPPAGE": _NUM, "NEXUS_MAX_SIGNAL_DRIFT_BPS": _NUM,
    "MIN_STOP_LIQ_GAP_PCT": _NUM, "DEFAULT_MMR": _NUM,
    "CROSS_GEOMETRY_EXTRA_HEADROOM_PCT": _NUM, "CROSS_GEOMETRY_MIN_RETAINED_FRACTION": _NUM,
    "STARTING_EQUITY": _NUM, "RESEARCH_MAX_DRAWDOWN_LIMIT": _NUM, "RESEARCH_MAX_HOLD_BARS": (int,),
    "CONTRACT_SPEC_SOURCE": (str,),
}
DAILY_PNL_MODES = ("PRODUCTION_REALIZED_TODAY_PLUS_OPEN_UNREALIZED",
                   "EQUITY_ANCHORED_AT_UTC_MIDNIGHT")


class ManifestError(ValueError):
    """Replay configuration is missing, malformed or inconsistent (fail closed)."""


def canonical_sha256(values: dict) -> str:
    blob = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _check_types(values: dict) -> None:
    missing = sorted(set(REQUIRED) - set(values))
    extra = sorted(set(values) - set(REQUIRED))
    if missing:
        raise ManifestError(f"MANIFEST_MISSING_KEYS {missing}")
    if extra:
        raise ManifestError(f"MANIFEST_UNKNOWN_KEYS {extra}")
    for key, types in REQUIRED.items():
        v = values[key]
        if bool in types:
            ok = isinstance(v, bool)
        else:
            ok = isinstance(v, types) and not isinstance(v, bool)
        if not ok:
            raise ManifestError(f"MANIFEST_BAD_TYPE {key}={v!r}")
        if isinstance(v, float) and not math.isfinite(v):
            raise ManifestError(f"MANIFEST_NONFINITE {key}")
    if values["DAILY_PNL_SEMANTICS"] not in DAILY_PNL_MODES:
        raise ManifestError("MANIFEST_BAD_DAILY_PNL_SEMANTICS")


class ReplayManifest:
    def __init__(self, raw: dict, *, source: str):
        if not isinstance(raw, dict) or not isinstance(raw.get("values"), dict):
            raise ManifestError("MANIFEST_MALFORMED")
        entries = raw["values"]
        values: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        for key, entry in entries.items():
            if not isinstance(entry, dict) or "value" not in entry:
                raise ManifestError(f"MANIFEST_ENTRY_MALFORMED {key}")
            values[key] = entry["value"]
            provenance[key] = str(entry.get("provenance", "UNSPECIFIED"))
        _check_types(values)
        self.values = values
        self.provenance = provenance
        self.policy_source = str(raw.get("policy_source") or source)
        self.source = source
        self.sha256 = canonical_sha256(values)
        self.risk_policy()  # validates bounds now (fail closed)

    def __getitem__(self, key: str):
        return self.values[key]

    def risk_policy(self):
        from bot import risk_policy as rp
        v = self.values
        policy = rp.RiskPolicy(
            leverage=float(v["LEVERAGE"]), max_risk_pct=float(v["MAX_RISK_PCT"]),
            max_margin_pct=float(v["MAX_MARGIN_PCT"]), max_drawdown=float(v["MAX_DRAWDOWN"]),
            max_positions=int(v["MAX_POSITIONS"]),
            daily_stop_loss_pct=float(v["DAILY_STOP_LOSS_PCT"]),
            daily_stop_loss_abs=float(v["DAILY_STOP_LOSS"]),
            min_rr_ratio=float(v["MIN_RR_RATIO"]),
            operator_margin_cap_pct=float(v["OPERATOR_MARGIN_CAP_PCT"]),
            max_stop_stress_risk_rate=float(v["MAX_STOP_STRESS_RISK_RATE"]),
            expected_slippage_pct=float(v["NEXUS_EXPECTED_SLIPPAGE_PCT"]),
            post_target_risk_pct=float(v["POST_TARGET_RISK"]),
        )
        bad = policy.violations()
        if bad:
            raise ManifestError(f"MANIFEST_RISK_POLICY_INVALID {list(bad)}")
        return policy

    def exit_policy(self):
        from bot.nexus_oos_execution_parity import ExitPolicy
        v = self.values
        return ExitPolicy(
            partial_fraction=float(v["PARTIAL_TP_FRACTION"]),
            tp1_funding_buffer=float(v["TP1_FUNDING_BUFFER"]),
            rr_double_multiple=float(v["RR_DOUBLE_MULTIPLE"]),
            trailing_trigger=float(v["TRAILING_TRIGGER"]),
            trailing_lock=float(v["TRAILING_LOCK"]),
            research_max_hold_bars=int(v["RESEARCH_MAX_HOLD_BARS"]),
        )

    def runtime_values(self) -> dict:
        """Values production code would read in this process right now."""
        from bot.config import cfg
        from bot import nexus_ai, liquidation, pilot, pilot_exposure_capacity
        from bot import kucoin_contract_risk_hardening as krh
        from bot import kucoin_execution_model as kem
        from bot.pre_dispatch_guard import limits_from_env
        from bot.engine import TradingEngine
        return {
            "MIN_ENTRY_SCORE": int(cfg.MIN_ENTRY_SCORE),
            "POST_TARGET_SCORE": int(cfg.POST_TARGET_SCORE),
            "NEXUS_MIN_SCORE_EFFECTIVE": float(nexus_ai.MIN_SCORE),
            "FEE_MULTIPLIER": float(cfg.FEE_MULTIPLIER),
            "MIN_VOLUME_MULT": float(cfg.MIN_VOLUME_MULT),
            "MIN_RR_RATIO": float(cfg.MIN_RR_RATIO),
            "TRAILING_TRIGGER": float(cfg.TRAILING_TRIGGER),
            "TRAILING_LOCK": float(cfg.TRAILING_LOCK),
            "TAKER_FEE": float(kem.configured_taker_fee()),
            "BACKTEST_SLIPPAGE": float(kem.slippage_rate_for_symbol("BTCUSDT")),
            "NEXUS_MAX_SIGNAL_DRIFT_BPS": float(limits_from_env().max_signal_drift_bps),
            "MIN_STOP_LIQ_GAP_PCT": float(liquidation.MIN_GAP_PCT),
            "DEFAULT_MMR": float(liquidation.DEFAULT_MMR),
            "CROSS_GEOMETRY_EXTRA_HEADROOM_PCT": float(krh._EXTRA_LIQ_HEADROOM_PCT),
            "CROSS_GEOMETRY_MIN_RETAINED_FRACTION": float(krh._MIN_RETAINED_STOP_FRACTION),
            "MAX_CONSEC_LOSSES": int(TradingEngine._MAX_CONSEC_LOSSES),
            "CB_COOLDOWN_HOURS": int(TradingEngine._CB_COOLDOWN_HOURS),
            "PILOT_MAX_CONCURRENT_POSITIONS": int(pilot.PILOT_MAX_CONCURRENT_POSITIONS),
            "PILOT_MIN_AVAILABLE_EQUITY_RATIO": float(pilot_exposure_capacity.MIN_AVAILABLE_EQUITY_RATIO),
            "PILOT_MAX_POSITION_MARGIN_EQUITY_RATIO": float(
                pilot_exposure_capacity.MAX_POSITION_MARGIN_EQUITY_RATIO),
            "DAILY_TARGET_PCT": float(cfg.DAILY_TARGET_PCT),
            "DAILY_TARGET": float(cfg.DAILY_TARGET),
        }

    def verify_runtime(self) -> dict:
        """Fail closed when an in-process production value differs."""
        runtime = self.runtime_values()
        mismatches = {k: {"manifest": self.values[k], "runtime": v}
                      for k, v in runtime.items()
                      if not math.isclose(float(self.values[k]), float(v), rel_tol=0, abs_tol=1e-12)}
        if mismatches:
            raise ManifestError(f"MANIFEST_RUNTIME_MISMATCH {mismatches}")
        return {"verified_keys": sorted(runtime), "mismatches": {}}

    def code_defaults(self) -> dict:
        """Portfolio-only keys whose production value is not in-process: show
        where the manifest differs from the code default, for the report."""
        from bot import risk_policy as rp
        return {
            "LEVERAGE": 10, "MAX_RISK_PCT": 0.01, "MAX_MARGIN_PCT": 0.10, "MAX_DRAWDOWN": 0.10,
            "MAX_POSITIONS": 2, "DAILY_STOP_LOSS_PCT": 0.03, "DAILY_STOP_LOSS": 0.0,
            "OPERATOR_MARGIN_CAP_PCT": rp.DEFAULT_OPERATOR_MARGIN_CAP_PCT,
            "MAX_STOP_STRESS_RISK_RATE": rp.DEFAULT_MAX_STOP_STRESS_RISK_RATE,
            "NEXUS_EXPECTED_SLIPPAGE_PCT": rp.DEFAULT_EXPECTED_SLIPPAGE_PCT,
            "POST_TARGET_RISK": 0.005,
        }

    def report(self) -> dict:
        defaults = self.code_defaults()
        return {
            "policy_source": self.policy_source,
            "policy_sha256": self.sha256,
            "values": dict(sorted(self.values.items())),
            "provenance": dict(sorted(self.provenance.items())),
            "differs_from_code_default": {k: {"manifest": self.values[k], "code_default": d}
                                          for k, d in defaults.items()
                                          if float(self.values[k]) != float(d)},
            "secrets_read": False,
        }


def load(path: str | Path | None = None) -> ReplayManifest:
    p = Path(path) if path else DEFAULT_PATH
    if not p.is_file():
        raise ManifestError(f"MANIFEST_NOT_FOUND {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"MANIFEST_UNREADABLE {type(exc).__name__}") from exc
    try:
        rel = str(p.resolve().relative_to(DEFAULT_PATH.parent.parent))
    except ValueError:
        rel = str(p)
    return ReplayManifest(raw, source=rel)
