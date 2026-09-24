"""Non-secret LIVE policy attestation (read-only, never mutates anything).

Problem: the committed replay manifest proves only that the CI runtime equals
the manifest. It does not prove that the manifest equals the LIVE runtime,
whose Railway variables are not readable from the repository.

Mechanism: the running bot logs ONE line at startup

    [NON_SECRET_POLICY_ATTESTATION] sha256=<hex> LEVERAGE=50 MAX_RISK_PCT=0.01 ...

containing ONLY the whitelisted policy keys below, each rendered canonically,
plus the sha256 of that canonical payload. The replay computes the same
canonical payload from its manifest and compares hashes. The operator copies
the line from the LIVE logs into a file and passes it to the replay
(``--live-policy-attestation``); nothing is fetched from Railway.

Security: values come from an explicit whitelist of numeric policy settings.
No environment variable is enumerated, and API keys, URLs, tokens and
passwords can never appear. A value that cannot be read renders as
``UNAVAILABLE`` (the hash then cannot match a manifest).
"""
from __future__ import annotations

import hashlib
import math
import re

TAG = "[NON_SECRET_POLICY_ATTESTATION]"

# Whitelist. Every key is a numeric/boolean policy value; order is fixed.
ATTESTED_KEYS: tuple[str, ...] = (
    "LEVERAGE", "MAX_RISK_PCT", "MAX_MARGIN_PCT", "MAX_DRAWDOWN", "MAX_POSITIONS",
    "DAILY_STOP_LOSS_PCT", "DAILY_STOP_LOSS", "MIN_RR_RATIO", "OPERATOR_MARGIN_CAP_PCT",
    "MAX_STOP_STRESS_RISK_RATE", "NEXUS_EXPECTED_SLIPPAGE_PCT", "POST_TARGET_RISK",
    "MIN_ENTRY_SCORE", "POST_TARGET_SCORE", "NEXUS_MIN_SCORE_EFFECTIVE", "FEE_MULTIPLIER",
    "MIN_VOLUME_MULT", "DAILY_TARGET_PCT", "DAILY_TARGET", "TRAILING_TRIGGER", "TRAILING_LOCK",
    "TAKER_FEE", "NEXUS_MAX_SIGNAL_DRIFT_BPS", "MIN_STOP_LIQ_GAP_PCT", "DEFAULT_MMR",
    "MAX_CONSEC_LOSSES", "CB_COOLDOWN_HOURS", "PILOT_MAX_CONCURRENT_POSITIONS",
    "PILOT_MIN_AVAILABLE_EQUITY_RATIO", "PILOT_MAX_POSITION_MARGIN_EQUITY_RATIO",
)
_VALUE_RE = re.compile(r"^(-?[0-9.eE+-]+|true|false|UNAVAILABLE)$")

RUNTIME_ERRORS: dict = {}

# Stage A (research/code merge): no LIVE line yet. Allowed by the research
# gate; the LIVE_RELEASE_GATE requires STATUS_MATCH.
STATUS_NOT_ATTESTED = "LIVE_ATTESTATION_PENDING"
STATUS_MATCH = "ATTESTED_MATCH"
STATUS_MISMATCH = "ATTESTED_MISMATCH"
STATUS_INVALID = "ATTESTATION_INVALID"


def render(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "UNAVAILABLE"
    if not math.isfinite(x):
        return "UNAVAILABLE"
    return format(x, ".10g")


def canonical(values: dict) -> str:
    return " ".join(f"{k}={render(values.get(k))}" for k in ATTESTED_KEYS)


def digest(values: dict) -> str:
    return hashlib.sha256(canonical(values).encode("utf-8")).hexdigest()


def attestation_line(values: dict) -> str:
    return f"{TAG} sha256={digest(values)} {canonical(values)}"


def runtime_policy() -> dict:
    """Whitelisted values as the running process sees them."""
    out: dict = {}
    errors: dict = {}

    def put(key, fn):
        try:
            out[key] = fn()
        except Exception as exc:  # rendered UNAVAILABLE; reported by install()
            out[key] = None
            errors[key] = type(exc).__name__

    from bot.config import cfg
    from bot import risk_policy as rp
    pol = None
    try:
        pol = rp.load_policy(cfg)
    except Exception as exc:  # every risk key then renders UNAVAILABLE
        errors["RISK_POLICY"] = type(exc).__name__
    for key, attr in (("LEVERAGE", "leverage"), ("MAX_RISK_PCT", "max_risk_pct"),
                      ("MAX_MARGIN_PCT", "max_margin_pct"), ("MAX_DRAWDOWN", "max_drawdown"),
                      ("MAX_POSITIONS", "max_positions"),
                      ("DAILY_STOP_LOSS_PCT", "daily_stop_loss_pct"),
                      ("DAILY_STOP_LOSS", "daily_stop_loss_abs"), ("MIN_RR_RATIO", "min_rr_ratio"),
                      ("OPERATOR_MARGIN_CAP_PCT", "operator_margin_cap_pct"),
                      ("MAX_STOP_STRESS_RISK_RATE", "max_stop_stress_risk_rate"),
                      ("NEXUS_EXPECTED_SLIPPAGE_PCT", "expected_slippage_pct"),
                      ("POST_TARGET_RISK", "post_target_risk_pct")):
        put(key, lambda a=attr: getattr(pol, a))
    put("MIN_ENTRY_SCORE", lambda: cfg.MIN_ENTRY_SCORE)
    put("POST_TARGET_SCORE", lambda: cfg.POST_TARGET_SCORE)
    put("NEXUS_MIN_SCORE_EFFECTIVE", lambda: __import__("bot.nexus_ai", fromlist=["MIN_SCORE"]).MIN_SCORE)
    put("FEE_MULTIPLIER", lambda: cfg.FEE_MULTIPLIER)
    put("MIN_VOLUME_MULT", lambda: cfg.MIN_VOLUME_MULT)
    put("DAILY_TARGET_PCT", lambda: cfg.DAILY_TARGET_PCT)
    put("DAILY_TARGET", lambda: cfg.DAILY_TARGET)
    put("TRAILING_TRIGGER", lambda: cfg.TRAILING_TRIGGER)
    put("TRAILING_LOCK", lambda: cfg.TRAILING_LOCK)
    put("TAKER_FEE", lambda: __import__("bot.kucoin_execution_model", fromlist=["x"]).configured_taker_fee())
    put("NEXUS_MAX_SIGNAL_DRIFT_BPS",
        lambda: __import__("bot.pre_dispatch_guard", fromlist=["x"]).limits_from_env().max_signal_drift_bps)
    put("MIN_STOP_LIQ_GAP_PCT", lambda: __import__("bot.liquidation", fromlist=["x"]).MIN_GAP_PCT)
    put("DEFAULT_MMR", lambda: __import__("bot.liquidation", fromlist=["x"]).DEFAULT_MMR)
    put("MAX_CONSEC_LOSSES", lambda: __import__("bot.engine", fromlist=["x"]).TradingEngine._MAX_CONSEC_LOSSES)
    put("CB_COOLDOWN_HOURS", lambda: __import__("bot.engine", fromlist=["x"]).TradingEngine._CB_COOLDOWN_HOURS)
    put("PILOT_MAX_CONCURRENT_POSITIONS",
        lambda: __import__("bot.pilot", fromlist=["x"]).PILOT_MAX_CONCURRENT_POSITIONS)
    put("PILOT_MIN_AVAILABLE_EQUITY_RATIO",
        lambda: __import__("bot.pilot_exposure_capacity", fromlist=["x"]).MIN_AVAILABLE_EQUITY_RATIO)
    put("PILOT_MAX_POSITION_MARGIN_EQUITY_RATIO",
        lambda: __import__("bot.pilot_exposure_capacity", fromlist=["x"]).MAX_POSITION_MARGIN_EQUITY_RATIO)
    RUNTIME_ERRORS.clear()
    RUNTIME_ERRORS.update(errors)
    return out


def manifest_values(manifest) -> dict:
    return {k: manifest.values.get(k) for k in ATTESTED_KEYS}


def parse(line: str) -> dict:
    """Parse and verify one attestation line. Raises ValueError if invalid."""
    text = str(line).strip()
    idx = text.find(TAG)
    if idx < 0:
        raise ValueError("attestation tag not found")
    parts = text[idx + len(TAG):].split()
    if not parts or not parts[0].startswith("sha256="):
        raise ValueError("sha256 missing")
    claimed = parts[0][len("sha256="):]
    kv = {}
    for token in parts[1:]:
        if "=" not in token:
            raise ValueError("malformed token")
        k, v = token.split("=", 1)
        if k not in ATTESTED_KEYS or not _VALUE_RE.match(v):
            raise ValueError(f"unexpected key or value: {k}")
        kv[k] = v
    if tuple(kv) != ATTESTED_KEYS:
        raise ValueError("key set/order differs from the whitelist")
    payload = " ".join(f"{k}={kv[k]}" for k in ATTESTED_KEYS)
    if hashlib.sha256(payload.encode("utf-8")).hexdigest() != claimed:
        raise ValueError("sha256 does not match payload")
    return {"sha256": claimed, "values": kv}


def compare(manifest, attestation_text: str | None) -> dict:
    replay_sha = digest(manifest_values(manifest))
    base = {"replay_policy_sha": replay_sha, "attested_keys": list(ATTESTED_KEYS),
            "secrets_included": False}
    if not attestation_text:
        return {**base, "status": STATUS_NOT_ATTESTED, "live_policy_sha": None,
                "note": "no LIVE attestation supplied; manifest values are PRODUCTION_REPORTED or "
                        "CODE_DEFAULT only"}
    try:
        live = parse(attestation_text)
    except ValueError as exc:
        return {**base, "status": STATUS_INVALID, "live_policy_sha": None, "error": str(exc)}
    mine = {k: render(v) for k, v in manifest_values(manifest).items()}
    mismatches = {k: {"replay": mine[k], "live": live["values"][k]}
                  for k in ATTESTED_KEYS if mine[k] != live["values"][k]}
    return {**base, "status": STATUS_MATCH if live["sha256"] == replay_sha else STATUS_MISMATCH,
            "live_policy_sha": live["sha256"], "mismatches": mismatches}


def install(log) -> str:
    """Log the attestation line once at startup (visible on failure)."""
    try:
        line = attestation_line(runtime_policy())
    except Exception as exc:
        log.error("%s status=UNAVAILABLE error=%s", TAG, type(exc).__name__)
        return ""
    log.warning(line)
    if RUNTIME_ERRORS:
        log.error("%s unavailable_keys=%s", TAG, sorted(RUNTIME_ERRORS))
    return line
