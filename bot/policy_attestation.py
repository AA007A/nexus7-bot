"""Non-secret LIVE policy OBSERVATION (read-only, never mutates anything).

What this is: at startup the bot logs ONE sanitized line

    [LIVE_POLICY_OBSERVATION_V2] format=2 candidate_sha=<git sha>
        deployment_id=<id> service_id_hash=<sha256> environment_id_hash=<sha256>
        generated_at=<UTC ISO> policy_sha256=<hex> LEVERAGE=50 MAX_RISK_PCT=0.01 ...

with the whitelisted policy values, the sha256 of their canonical payload and
the code/deployment identity the platform exposes to the process
(RAILWAY_GIT_COMMIT_SHA, RAILWAY_DEPLOYMENT_ID; service/environment IDs are
hashed). The replay recomputes the same canonical payload from its manifest.

What this is NOT: authentication. sha256 of a plaintext payload proves only
payload INTEGRITY. Anyone who knows the policy values can build the same line.
A matching line is therefore reported as ``POLICY_CONTENT_MATCH`` with
``provenance = UNAUTHENTICATED``. Source provenance (the line came from THIS
deployment of THIS commit in THIS environment) is established only by the
Stage-C release verifier (``bot/live_release_evidence.py``), which reads the
deployment metadata and that deployment's logs from a trusted, read-only
control-plane source. A line supplied to the replay is a research fixture.

Security: values come from an explicit whitelist of numeric policy settings
plus four platform identity variables. No other environment variable is
enumerated; API keys, URLs, tokens and passwords can never appear. A value that
cannot be read renders as ``UNAVAILABLE``.
"""
from __future__ import annotations

import hashlib
import math
import re

TAG = "[LIVE_POLICY_OBSERVATION_V2]"
FORMAT_VERSION = "2"
IDENTITY_FIELDS = ("format", "candidate_sha", "deployment_id", "service_id_hash",
                   "environment_id_hash", "generated_at", "policy_sha256")
_IDENTITY_RE = {
    "format": re.compile(r"^2$"),
    "candidate_sha": re.compile(r"^([0-9a-f]{40}|UNAVAILABLE)$"),
    "deployment_id": re.compile(r"^([A-Za-z0-9-]{1,64}|UNAVAILABLE)$"),
    "service_id_hash": re.compile(r"^([0-9a-f]{64}|UNAVAILABLE)$"),
    "environment_id_hash": re.compile(r"^([0-9a-f]{64}|UNAVAILABLE)$"),
    "generated_at": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"),
    "policy_sha256": re.compile(r"^[0-9a-f]{64}$"),
}

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

# Content comparison only. None of these states is provenance.
STATUS_PENDING = "LIVE_POLICY_OBSERVATION_PENDING"   # no observation supplied (stage A)
STATUS_MATCH = "POLICY_CONTENT_MATCH"
STATUS_MISMATCH = "POLICY_CONTENT_MISMATCH"
STATUS_INVALID = "POLICY_OBSERVATION_INVALID"
PROVENANCE_UNAUTHENTICATED = "UNAUTHENTICATED"


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


def id_hash(value) -> str:
    """sha256 of a platform identifier (service/environment IDs are not logged raw)."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest() if value else "UNAVAILABLE"


def runtime_identity(env=None, now=None) -> dict:
    """Code/deployment identity exposed by the platform. Read-only; no secrets."""
    import datetime as _dt
    import os
    env = os.environ if env is None else env
    sha = str(env.get("RAILWAY_GIT_COMMIT_SHA") or "").strip().lower()
    dep = str(env.get("RAILWAY_DEPLOYMENT_ID") or "").strip()
    ident = {
        "format": FORMAT_VERSION,
        "candidate_sha": sha if _IDENTITY_RE["candidate_sha"].match(sha or "-") else "UNAVAILABLE",
        "deployment_id": dep if _IDENTITY_RE["deployment_id"].match(dep or "-") and dep else "UNAVAILABLE",
        "service_id_hash": id_hash(str(env.get("RAILWAY_SERVICE_ID") or "").strip()),
        "environment_id_hash": id_hash(str(env.get("RAILWAY_ENVIRONMENT_ID") or "").strip()),
        "generated_at": (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return ident


def observation_line(values: dict, identity: dict) -> str:
    ident = {**identity, "policy_sha256": digest(values)}
    head = " ".join(f"{k}={ident[k]}" for k in IDENTITY_FIELDS)
    return f"{TAG} {head} {canonical(values)}"


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
    """Parse one observation line and verify payload INTEGRITY only.

    Raises ValueError if malformed. Success says nothing about who wrote it.
    """
    text = str(line).strip()
    idx = text.find(TAG)
    if idx < 0:
        raise ValueError("observation tag not found")
    tokens = text[idx + len(TAG):].split()
    ident, kv = {}, {}
    for token in tokens:
        if "=" not in token:
            raise ValueError("malformed token")
        k, v = token.split("=", 1)
        if k in _IDENTITY_RE:
            if k in ident or not _IDENTITY_RE[k].match(v):
                raise ValueError(f"bad identity field: {k}")
            ident[k] = v
        elif k in ATTESTED_KEYS and _VALUE_RE.match(v):
            kv[k] = v
        else:
            raise ValueError(f"unexpected key or value: {k}")
    if tuple(ident) != IDENTITY_FIELDS:
        raise ValueError("identity fields missing or out of order")
    if tuple(kv) != ATTESTED_KEYS:
        raise ValueError("key set/order differs from the whitelist")
    payload = " ".join(f"{k}={kv[k]}" for k in ATTESTED_KEYS)
    if hashlib.sha256(payload.encode("utf-8")).hexdigest() != ident["policy_sha256"]:
        raise ValueError("policy_sha256 does not match payload")
    return {"sha256": ident["policy_sha256"], "identity": ident, "values": kv}


def find_observations(lines) -> list[dict]:
    """Every valid observation in ``lines`` (invalid ones are skipped)."""
    out = []
    for line in lines or ():
        if TAG in str(line):
            try:
                out.append(parse(line))
            except ValueError:
                continue
    return out


def compare(manifest, observation_text: str | None) -> dict:
    """POLICY CONTENT comparison against the replay manifest (never provenance)."""
    replay_sha = digest(manifest_values(manifest))
    base = {"replay_policy_sha": replay_sha, "attested_keys": list(ATTESTED_KEYS),
            "secrets_included": False, "provenance": PROVENANCE_UNAUTHENTICATED,
            "meaning": "payload integrity/content only; source provenance is verified only "
                       "by the Stage-C release verifier from trusted control-plane data"}
    if not observation_text:
        return {**base, "status": STATUS_PENDING, "live_policy_sha": None,
                "note": "no LIVE policy observation supplied; manifest values are "
                        "PRODUCTION_REPORTED or CODE_DEFAULT only"}
    try:
        live = parse(observation_text)
    except ValueError as exc:
        return {**base, "status": STATUS_INVALID, "live_policy_sha": None, "error": str(exc)}
    mine = {k: render(v) for k, v in manifest_values(manifest).items()}
    mismatches = {k: {"replay": mine[k], "live": live["values"][k]}
                  for k in ATTESTED_KEYS if mine[k] != live["values"][k]}
    return {**base, "status": STATUS_MATCH if live["sha256"] == replay_sha else STATUS_MISMATCH,
            "live_policy_sha": live["sha256"], "observation_identity": live["identity"],
            "source": "RESEARCH_FIXTURE", "mismatches": mismatches}


def install(log) -> str:
    """Log the observation line once at startup (visible on failure)."""
    try:
        line = observation_line(runtime_policy(), runtime_identity())
    except Exception as exc:
        log.error("%s status=UNAVAILABLE error=%s", TAG, type(exc).__name__)
        return ""
    log.warning(line)
    if RUNTIME_ERRORS:
        log.error("%s unavailable_keys=%s", TAG, sorted(RUNTIME_ERRORS))
    return line
