"""Isolated AI SHADOW observer — forward evidence with ZERO exchange mutation.

``AI_EXECUTION_MODE=SHADOW`` inside the production engine only means the AI
itself has no order authority; the legacy engine in that process can still
trade. Forward SHADOW evidence therefore comes from THIS separate process:

  * it never constructs TradingEngine, never acquires the LIVE execution
    lease, never touches production positions;
  * its only exchange client is the public, GET-only
    ``PublicKuCoinFuturesClient`` (authenticated calls raise);
  * it refuses to start when any exchange credential is present in its
    environment (KUCOIN_API_KEY / _SECRET / _PASSPHRASE) or when
    AI_EXECUTION_MODE != SHADOW;
  * evidence goes ONLY to a dedicated PostgreSQL evidence DB
    (bot.ai.evidence_store; SQLite / production DB refused); every hook
    candidate (TRADE and ABSTAIN) is persisted once and its production-parity
    outcome is resolved later on closed candles (bot.ai.forward_collector);
  * the symbol universe is the pinned 12-symbol production universe;
  * it reproduces AI_RUNTIME_HOOK_POPULATION_V1 (LIVE pilot profile) from
    public data with the SAME primitives as the replay: Analyzer.analyze_mtf,
    the scan funnel, nexus_ai.decide, production CROSS geometry with the
    public maintainMargin MMR proxy (APPROXIMATED, as in the replay), NEXUS
    recheck, bot.ai.hook.evaluate_candidate, canonical features, and the
    pinned bundle via AIRuntime (SHADOW).

Run (future isolated service only; never in the production service):
    python -m bot.ai.shadow_observer
"""
from __future__ import annotations

import asyncio
import os
import re
import time

FORBIDDEN_CREDENTIALS = ("KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_API_PASSPHRASE")
REQUIRED_ENV = {"AI_EXECUTION_MODE": "SHADOW"}
MUTATING_NAMES = ("place_order", "cancel_order", "cancel_all", "set_position_stops", "set_leverage",
                  "close_position", "create_order", "post", "put", "delete")


class ObserverRefused(RuntimeError):
    pass


def preflight(env) -> None:
    """Fail closed before any network or DB access."""
    present = [k for k in FORBIDDEN_CREDENTIALS if str(env.get(k, "") or "").strip()]
    if present:
        raise ObserverRefused(f"exchange credentials present: {present}")
    for k, v in REQUIRED_ENV.items():
        if str(env.get(k, "")).strip().upper() != v:
            raise ObserverRefused(f"{k} must be {v}")


def assert_read_only(client) -> None:
    """The observer's exchange client exposes no mutation capability."""
    for name in MUTATING_NAMES:
        if callable(getattr(client, name, None)):
            raise ObserverRefused(f"client exposes mutating method {name}")


class _EvidenceOnlyJournal:
    """The observer never writes bot.database (which may fall back to SQLite):
    its durable record is the evidence store's ai_shadow_candidates row."""

    async def put(self, *a, **k):
        return True

    async def get(self, *a, **k):
        return None

    async def open_decisions(self):
        return []

    async def set_status(self, *a, **k):
        return True


def _ms(c) -> int:
    t = int(c.get("ts", 0) or 0)
    return t * 1000 if t < 100_000_000_000 else t


def decision_inputs(k15, k1h, k4h, decision_ts: int) -> dict:
    """V3 decision_data_rule: the decision candle (open_ts == decision_ts) must
    exist and supply the ticker open (NO previous-close fallback); the last
    closed 15m/1h/4h bars must be the ones immediately before decision_ts."""
    d = int(decision_ts)
    h1, h4 = 3_600_000, 4 * 3_600_000
    dc = [c for c in (k15 or []) if _ms(c) == d]
    if not dc:
        return {"ok": False, "reason": "DECISION_CANDLE_MISSING"}

    def last_closed(bars, iv):
        xs = [_ms(c) for c in (bars or []) if _ms(c) + iv <= d]
        return max(xs) if xs else None
    if last_closed(k15, M15) != d - M15:
        return {"ok": False, "reason": "STALE_15M"}
    if last_closed(k1h, h1) != (d // h1) * h1 - h1:
        return {"ok": False, "reason": "STALE_1H"}
    if last_closed(k4h, h4) != (d // h4) * h4 - h4:
        return {"ok": False, "reason": "STALE_4H"}
    return {"ok": True, "ticker": {"lastPrice": str(float(dc[0]["o"]))}}


M15 = 15 * 60_000
_STAGE_TO_BOUNDARY = {"AI_HOOK": "AI_HOOK", "NO_SIGNAL": "NO_SIGNAL", "NEXUS_REJECTED": "NEXUS_REJECTED",
                      "NEXUS_RECHECK_REJECTED": "NEXUS_REJECTED", "DATA_MISSING": "DATA_MISSING"}


def boundary_status(result: dict) -> str:
    stage = result.get("stage")
    if stage == "AI_HOOK" and not result.get("persisted"):
        return "ERROR"
    return _STAGE_TO_BOUNDARY.get(stage, "ERROR" if stage == "ERROR" else "COMPLETE")


SHA40 = re.compile(r"[0-9a-f]{40}")


def resolve_code_sha(env) -> str:
    """Runtime code identity. On Railway, RAILWAY_GIT_COMMIT_SHA is the
    authority; CANDIDATE_SHA (offline) may never override it and must agree
    when both are set. Only an exact 40-lowercase-hex SHA is accepted."""
    railway = str(env.get("RAILWAY_GIT_COMMIT_SHA", "") or "").strip()
    cand = str(env.get("CANDIDATE_SHA", "") or "").strip()
    if railway and cand and railway != cand:
        raise ObserverRefused("RAILWAY_GIT_COMMIT_SHA and CANDIDATE_SHA disagree")
    sha = railway or cand
    if not SHA40.fullmatch(sha):
        raise ObserverRefused("code sha must be exactly 40 lowercase hex characters")
    return sha


def _pinned(env, key) -> str:
    v = str(env.get(key, "") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", v):
        raise ObserverRefused(f"{key} must be deploy-pinned (64 lowercase hex)")
    return v


def _load_metadata(runtime_env) -> dict:
    import json
    from pathlib import Path
    try:
        meta = json.loads((Path(str(runtime_env.get("AI_BUNDLE_DIR", ""))) / "bundle_metadata.json").read_text())
    except Exception as exc:
        raise ObserverRefused(f"BUNDLE_METADATA_INVALID: unreadable ({type(exc).__name__})") from exc
    if not isinstance(meta, dict):
        raise ObserverRefused("BUNDLE_METADATA_INVALID: malformed")
    return meta


def verify_runtime_parity(manifest, env) -> dict:
    """The observer's in-process production values must equal the pinned replay
    policy manifest (verify_runtime) and its sha must equal the deploy pin.
    Cost parity is asserted explicitly (fees/slippage move AI costs, forward R
    and cost stress). Any mismatch refuses startup: no window, no observation."""
    from bot import nexus_oos_replay_manifest as rm
    from bot.kucoin_execution_model import configured_taker_fee, slippage_rate_for_symbol
    if manifest is None:
        raise ObserverRefused("replay policy manifest not loaded")
    if _pinned(env, "SHADOW_REPLAY_POLICY_SHA256") != manifest.sha256:
        raise ObserverRefused("REPLAY_POLICY_SHA_MISMATCH: loaded manifest differs from the deploy pin")
    try:
        res = manifest.verify_runtime()
    except rm.ManifestError as exc:
        raise ObserverRefused(f"RUNTIME_MANIFEST_PARITY_FAILED: {exc}") from exc
    if float(configured_taker_fee()) != float(manifest["TAKER_FEE"]):
        raise ObserverRefused("RUNTIME_MANIFEST_PARITY_FAILED: TAKER_FEE")
    base = float(slippage_rate_for_symbol("BTCUSDT"))
    if base != float(manifest["BACKTEST_SLIPPAGE"]):
        raise ObserverRefused("RUNTIME_MANIFEST_PARITY_FAILED: BACKTEST_SLIPPAGE")
    for sym in ("XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "NEARUSDT",
                "ATOMUSDT"):
        if float(slippage_rate_for_symbol(sym)) != base * 2.0:
            raise ObserverRefused(f"RUNTIME_MANIFEST_PARITY_FAILED: non-major slippage rule ({sym})")
    return {"status": "PASS", "verified_keys": sorted(res["verified_keys"]),
            "replay_policy_manifest_sha256": manifest.sha256}


class ShadowObserver:
    def __init__(self, env=None, *, client=None, runtime=None, manifest=None, symbols=None, store=None,
                 log=None):
        from bot.ai import forward_evidence as fe
        self.env = dict(os.environ if env is None else env)
        preflight(self.env)                                           # 1. no credentials
        self.steps = ["NO_CREDENTIALS"]
        from bot.nexus_oos_real_replay import PublicKuCoinFuturesClient
        self.client = client or PublicKuCoinFuturesClient()
        assert_read_only(self.client)                                 # 2. read-only public client
        self.steps.append("READ_ONLY_CLIENT")
        self.symbols = list(symbols or str(self.env.get("SHADOW_SYMBOLS", "")).split())
        if self.symbols != list(fe.SHADOW_UNIVERSE):                  # 3. exact ordered universe
            raise ObserverRefused(f"SHADOW_SYMBOLS must equal the pinned universe in its exact order "
                                  f"{list(fe.SHADOW_UNIVERSE)}; got {self.symbols}")
        self.steps.append("ORDERED_UNIVERSE")
        self.runtime = runtime
        self.manifest = manifest
        self.store = store
        self.window = None
        self.closed = False
        self.mmr_proxy: dict = {}
        self.log = log
        self.code_sha = None                                          # set only after verification (start)
        self.runtime_parity = None
        self.bundle_metadata = None
        self.health = {"observer_running": False, "last_successful_scan_ms": None,
                       "last_completed_boundary_ms": None, "db_durable": False, "bundle_verified": False,
                       "policy_verified": False, "schema_verified": False, "symbols_configured": list(self.symbols),
                       "pending_candidates": 0, "resolved_candidates": 0, "data_gaps": 0,
                       "evidence_continuity_broken": False, "window_id": None, "window_status": None}

    @property
    def orders_sent(self) -> int:
        return 0                        # there is no sender; this can never change

    @orders_sent.setter
    def orders_sent(self, value):
        raise ObserverRefused("orders_sent is structurally 0 in the SHADOW observer")

    def safety(self) -> dict:
        creds = any(str(self.env.get(k, "") or "").strip() for k in FORBIDDEN_CREDENTIALS)
        try:
            assert_read_only(self.client)
            mutating = False
        except ObserverRefused:
            mutating = True
        return {"exchange_credentials_present": creds, "mutating_client_methods": mutating,
                "execution_lease_acquired": False, "orders_sent": self.orders_sent}

    def start(self) -> None:
        """Steps 4-10: PostgreSQL, isolation, bundle, policy/schema/metadata,
        contract V3 (deploy-pinned), replay-manifest runtime parity
        (deploy-pinned), exact code provenance. Any failure refuses startup."""
        from bot.ai import bundle_export as bx
        from bot.ai import features as fx
        from bot.ai import forward_evidence as fe
        from bot.ai import hook as ai_hook
        from bot.ai.runtime import AIRuntime
        code_sha = resolve_code_sha(self.env)
        if self.store is None or getattr(self.store, "backend", None) != "postgresql" \
                and not getattr(self.store, "allow", False):
            raise ObserverRefused("evidence mode requires the durable PostgreSQL evidence store")
        self.steps.append("POSTGRESQL_CONNECTED")
        if not (self.store.identity or {}).get("authority_id") or not (self.store.identity or {}).get("fingerprint"):
            raise ObserverRefused("evidence DB isolation not verified")
        self.steps.append("DB_ISOLATION_VERIFIED")
        # 6. bundle (runtime loader) + SHADOW-safe lifecycle for this isolated process
        if self.runtime is None:
            self.runtime = AIRuntime(self.env, paper_trade=True, log=self.log, journal=_EvidenceOnlyJournal())
        if self.runtime.mode != "SHADOW":
            raise ObserverRefused("observer runtime must be SHADOW")
        self.runtime.startup(candidate_sha=code_sha, hook_profile=ai_hook.PROFILE_LIVE_PILOT)
        if self.runtime.halts.halted or self.runtime.bundle is None:
            raise ObserverRefused(f"bundle not verified: {sorted(self.runtime.halts.active)}")
        b = self.runtime.bundle
        if b.manifest.get("lifecycle_state") not in bx.SHADOW_LIFECYCLES:
            raise ObserverRefused(f"lifecycle {b.manifest.get('lifecycle_state')} refused by the isolated "
                                  f"observer (only {list(bx.SHADOW_LIFECYCLES)})")
        self.steps.append("BUNDLE_LOADED")
        # 7. policy / schema / bundle_metadata.json
        pol_ok = b.policy.sha256 == b.manifest["decision_policy_sha256"]
        sch_ok = b.manifest["feature_schema_sha256"] == fx.schema_hash()
        if not (pol_ok and sch_ok):
            raise ObserverRefused("policy/schema not verified")
        self.bundle_metadata = _load_metadata(self.runtime.env)
        try:
            bx.verify_metadata(self.bundle_metadata, b.manifest)
        except bx.BundleExportError as exc:
            raise ObserverRefused(f"BUNDLE_METADATA_INVALID: {exc}") from exc
        self.steps.append("POLICY_SCHEMA_VERIFIED")
        # 8. contract V3, deploy-pinned
        c = fe.SHADOW_CONTRACT
        if c["name"] != "FORWARD_SHADOW_EVIDENCE_V3" or fe._sha(c) != fe.CONTRACT_SHA256["SHADOW"] or \
                c["symbol_universe"] != self.symbols:
            raise ObserverRefused("forward contract V3 not verified")
        if _pinned(self.env, "SHADOW_FORWARD_CONTRACT_SHA256") != fe.CONTRACT_SHA256["SHADOW"]:
            raise ObserverRefused("FORWARD_CONTRACT_SHA_MISMATCH: deploy-pinned contract sha differs")
        self.steps.append("CONTRACT_V3_VERIFIED")
        # 9. replay policy manifest: deploy-pinned sha + runtime parity (incl. explicit cost parity)
        self.runtime_parity = verify_runtime_parity(self.manifest, self.env)
        self.steps.append("REPLAY_MANIFEST_RUNTIME_PARITY")
        # 10. exact code provenance: runtime code sha == bundle training sha == metadata candidate sha
        if not (code_sha == b.manifest.get("training_code_sha") == self.bundle_metadata.get("candidate_code_sha")):
            raise ObserverRefused("CODE_BUNDLE_SHA_MISMATCH: runtime code sha, bundle training_code_sha and "
                                  "bundle_metadata candidate_code_sha must be identical")
        if getattr(self.runtime.authority, "candidate_sha", None) != code_sha:
            raise ObserverRefused("AI decision authority not bound to the verified code sha")
        self.code_sha = code_sha
        self.steps.append("CODE_PROVENANCE_VERIFIED")
        self.health.update(observer_running=True, db_durable=getattr(self.store, "backend", "") == "postgresql",
                           bundle_verified=True, policy_verified=pol_ok, schema_verified=sch_ok,
                           bundle_metadata_verified=True, runtime_manifest_parity="PASS", code_sha=code_sha)

    def observation_line(self, deployment_id=None) -> str:
        """[AI_IDENTITY_OBSERVATION_V1] with the VERIFIED code sha (never null)."""
        if self.code_sha is None:
            raise ObserverRefused("code provenance not verified")
        return self.runtime.observation_line(candidate_sha=self.code_sha, deployment_id=deployment_id)

    # ── window identity / lifecycle ─────────────────────────────────────
    def cost_identity(self) -> dict:
        from bot.kucoin_execution_model import configured_taker_fee, slippage_rate_for_symbol
        from bot.nexus_oos_replay_manifest import canonical_sha256
        ep = self.manifest.exit_policy()
        return {"taker_fee": float(configured_taker_fee()),
                "slippage_model_version": SLIPPAGE_MODEL_VERSION,
                "slippage_rates": {s: float(slippage_rate_for_symbol(s)) for s in self.symbols},
                "exit_policy_sha256": canonical_sha256(ep.to_dict()),
                "replay_policy_manifest_sha256": self.manifest.sha256}

    def window_identity(self) -> dict:
        from bot.ai import forward_collector as fc
        from bot.ai import forward_evidence as fe
        b = self.runtime.bundle
        if self.code_sha is None or self.runtime_parity is None:
            raise ObserverRefused("provenance not verified; no window identity")
        return {"contract_name": fe.SHADOW_CONTRACT["name"], "contract_sha256": fe.CONTRACT_SHA256["SHADOW"],
                "forward_contract_sha256": fe.CONTRACT_SHA256["SHADOW"],
                "code_sha": self.code_sha, "bundle_sha256": b.sha256, "policy_sha256": b.policy.sha256,
                "feature_schema_sha256": b.manifest["feature_schema_sha256"],
                "training_dataset_manifest_sha256": b.manifest.get("training_dataset_manifest_sha256"),
                "bundle_lifecycle_state": b.manifest.get("lifecycle_state"),
                "replay_policy_manifest_sha256": self.manifest.sha256,
                "runtime_manifest_parity_status": self.runtime_parity["status"],
                "replay_runtime_verified_keys": list(self.runtime_parity["verified_keys"]),
                "hook_population": b.manifest.get("hook_population"), "hook_profile": b.manifest.get("hook_profile"),
                "symbol_universe": list(self.symbols), "symbol_universe_sha256": fe.universe_sha256(self.symbols),
                "evidence_db_authority_id": self.store.identity.get("authority_id"),
                "evidence_db_fingerprint": self.store.identity.get("fingerprint"),
                "cost_identity": self.cost_identity(), "collector_version": fc.COLLECTOR_VERSION}

    async def open_window(self, now_ms: int) -> dict:
        """Step 11: load the ACTIVE window (exact identity match) or create the
        first/explicitly requested one; committed before any observation."""
        from bot.ai import evidence_store as es
        from bot.ai import forward_evidence as fe
        if "CODE_PROVENANCE_VERIFIED" not in self.steps:
            raise ObserverRefused("startup sequence incomplete; window refused")
        ident = self.window_identity()
        act = await self.store.active_window()
        if act is not None:
            if act["identity"] != ident:
                diff = sorted(k for k in set(act["identity"]) | set(ident)
                              if act["identity"].get(k) != ident.get(k))
                await self.store.update_window(act["window_id"], status=es.W_INVALID,
                                               invalidated_reason={"identity_fields_changed": diff})
                raise ObserverRefused(f"window identity changed {diff}: window INVALID_IDENTITY_CHANGE; "
                                      "no append; a new window needs explicit initialization")
            self.window = act
        else:
            prior = await self.store.windows()
            if prior:
                latest = max(prior, key=lambda w: w["window_start_ms"])
                if str(self.env.get("AI_EVIDENCE_NEW_WINDOW_AFTER", "")).strip() != latest["window_id"]:
                    raise ObserverRefused("no ACTIVE window; a new window needs explicit initialization "
                                          "(AI_EVIDENCE_NEW_WINDOW_AFTER=<latest window_id>)")
            start = (int(now_ms) // fe.M15_MS + 1) * fe.M15_MS
            self.window = await self.store.create_window(
                ident, created_at_ms=int(now_ms), first_boundary_ms=start,
                window_end_ms=start + fe.WINDOW_DURATION_MS, expected_boundaries=fe.EXPECTED_BOUNDARIES)
        self.steps.append("WINDOW_COMMITTED")
        self.health.update(window_id=self.window["window_id"], window_status=self.window["status"],
                           evidence_continuity_broken=bool(self.window.get("continuity_broken")),
                           last_completed_boundary_ms=self.window.get("last_completed_boundary_ms"))
        if self.window.get("continuity_broken"):
            raise ObserverRefused(f"window continuity broken ({self.window.get('continuity_reason')}); "
                                  "collection halted")
        await self.detect_gaps(now_ms)
        return self.window

    async def _refresh(self):
        self.window = await self.store.window(self.window["window_id"])
        self.health.update(window_status=self.window["status"],
                           evidence_continuity_broken=bool(self.window.get("continuity_broken")),
                           last_completed_boundary_ms=self.window.get("last_completed_boundary_ms"))
        return self.window

    async def detect_gaps(self, now_ms: int) -> int:
        """Restart/outage gap detection: an interrupted boundary breaks
        continuity durably; every boundary whose deadline passed without a
        journal is recorded MISSED for all 12 symbols (never zero candidates)."""
        from bot.ai import forward_evidence as fe
        w = await self._refresh()
        wid = w["window_id"]
        if w.get("boundary_in_progress_ms") is not None:
            await self.store.mark_broken(wid, f"INTERRUPTED_BOUNDARY:{w['boundary_in_progress_ms']}")
            await self._refresh()
            raise ObserverRefused("previous run died inside a boundary; continuity broken")
        last = w.get("last_completed_boundary_ms")
        b = w["first_boundary_ms"] if last is None else int(last) + fe.M15_MS
        missed, last_missed = 0, None
        while b < w["window_end_ms"] and b + fe.MAX_DECISION_LATENESS_MS < int(now_ms):
            for sym in self.symbols:                     # idempotent: a re-run after a crash is a no-op
                await self.store.put_boundary(wid, b, sym, "MISSED", {"reason": "NOT_EVALUATED_BEFORE_DEADLINE"})
            missed, last_missed = missed + 1, b
            b += fe.M15_MS
        if missed:
            await self.store.update_window(wid, last_completed_boundary_ms=last_missed,
                                           missed_boundaries=int(w.get("missed_boundaries") or 0) + missed)
            await self.store.heartbeat(wid, {"ts": int(now_ms), "kind": "GAP", "missed_boundaries": missed})
        await self._refresh()
        self.health["data_gaps"] += missed
        return missed

    # ── one symbol ──────────────────────────────────────────────────────
    async def evaluate(self, symbol: str, k15, k1h, k4h, *, decision_ts: int, now_ms: int,
                       funding=None) -> dict:
        """One symbol at one 15m boundary, exactly as the replay does; every
        hook-eligible candidate is persisted (TRADE and ABSTAIN)."""
        from bot import nexus_ai
        from bot import nexus_oos_execution_parity as xp
        from bot.ai import forward_collector as fc
        from bot.ai import hook as ai_hook
        from bot.ai.runtime import runtime_costs
        from bot.backtest import _closed_window_by_ts, _timestamp_index
        from bot.config import cfg
        from bot.engine import TradingEngine
        from bot.kucoin_execution_model import estimated_round_trip_cost_pct
        from bot.nexus_oos_real_replay import _decide, _freeze_nexus_clock, runtime_nexus_threshold
        from bot.strategy import Analyzer
        if self.window is None:
            raise ObserverRefused("no committed evidence window; observation refused")
        man = self.manifest
        cost = self.window["identity"]["cost_identity"]
        di = decision_inputs(k15, k1h, k4h, decision_ts)
        if not di["ok"]:
            return {"symbol": symbol, "stage": "DATA_MISSING", "reason": "BOUNDARY_INPUT_INCOMPLETE",
                    "detail": di["reason"]}
        w15 = _closed_window_by_ts(k15, _timestamp_index(k15), decision_ts, 15, 80)
        w1h = _closed_window_by_ts(k1h, _timestamp_index(k1h), decision_ts, 60, 50)
        w4h = _closed_window_by_ts(k4h, _timestamp_index(k4h), decision_ts, 240, 30)
        if len(w15) < 60 or len(w1h) < 40 or len(w4h) < 20:
            return {"symbol": symbol, "stage": "DATA_MISSING", "reason": "BOUNDARY_INPUT_INCOMPLETE",
                    "detail": "INSUFFICIENT_HISTORY"}
        sig = Analyzer().analyze_mtf(symbol, w15, w1h, w4h, min_score=int(man["MIN_ENTRY_SCORE"]),
                                     fee_mult=float(man["FEE_MULTIPLIER"]),
                                     vol_mult=getattr(cfg, "MIN_VOLUME_MULT", 1.2))
        if not sig:
            return {"symbol": symbol, "stage": "NO_SIGNAL"}
        threshold = runtime_nexus_threshold(nexus_ai)
        ticker = di["ticker"]           # the open of the decision candle (replay parity); no fallback
        funnel = xp.funnel_flags(sig, decision_ts, TradingEngine)
        taker = float(cost["taker_fee"])
        cost_fraction = xp.production_cost_fraction(
            taker_fee=float(man["TAKER_FEE"]), expected_slippage_pct=float(man["NEXUS_EXPECTED_SLIPPAGE_PCT"]),
            modeled_round_trip_pct=estimated_round_trip_cost_pct(symbol, float(man["TAKER_FEE"])))
        with _freeze_nexus_clock(nexus_ai, decision_ts):
            nx = _decide(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding, threshold)
            geometry = xp.production_geometry(sig, leverage=int(man["LEVERAGE"]),
                                              mmr=self.mmr_proxy.get(symbol),
                                              fee_multiplier=float(man["FEE_MULTIPLIER"]))
            nx2 = None
            if geometry["status"] == "ADJUSTED" and getattr(nx, "execution_allowed", False) is True:
                adj = xp._SigView(sig, sl=geometry["sl"], tp=geometry["tp"])
                nx2 = _decide(nexus_ai, symbol, w15, w1h, w4h, adj, ticker, funding, threshold)
        hk = ai_hook.evaluate_candidate(
            sig, decision_ts=decision_ts, funnel=funnel, min_entry_score=float(man["MIN_ENTRY_SCORE"]),
            nx_initial=nx, geometry=geometry, nx_final=nx2, cost_fraction=cost_fraction,
            profile=ai_hook.PROFILE_LIVE_PILOT)
        if not hk["eligible"]:
            return {"symbol": symbol, "stage": hk["stage"]}
        obs = hk["observation"]
        out = await self.runtime.gate(obs, w15, w1h, w4h, taker_fee=taker, now_ms=now_ms)
        if out.decision is None:
            return {"symbol": symbol, "stage": "AI_HOOK", "reason": out.reason, "persisted": False}
        final_tp = geometry["tp"] if geometry.get("status") == "ADJUSTED" else float(sig.tp)
        costs = runtime_costs(cost_fraction=cost_fraction,
                              stop_distance_pct=abs(obs.entry - obs.stop) / obs.entry, taker_fee=taker)
        rec = fc.candidate_record(obs=obs, decision=out.decision, signal_tp=float(sig.tp), final_tp=float(final_tp),
                                  costs=costs, regime=out.decision.regime, code_sha=self.code_sha,
                                  bundle=self.runtime.bundle, fee_rate=taker,
                                  slippage_rate=float(cost["slippage_rates"][symbol]), now_ms=now_ms)
        persisted = await self.store.put_candidate(self.window["window_id"], rec)
        return {"symbol": symbol, "stage": "AI_HOOK", "reason": out.reason, "allow": out.allow,
                "decision_id": out.decision.decision_id, "candidate_id": rec["candidate_id"],
                "ai_decision": rec["ai_decision"], "persisted": persisted}

    async def _bars_since(self, symbol, start_ms, now_ms):
        from bot.backtest import fetch_history
        n = min(3000, max(200, int((now_ms - start_ms) // 900_000) + 8))
        return await fetch_history(self.client, symbol, "15", n)

    async def _funding_since(self, symbol, start_ms, now_ms):
        from bot.kucoin_execution_model import fetch_public_funding_history
        return await fetch_public_funding_history(self.client, symbol, int(start_ms) - 8 * 3_600_000, int(now_ms))

    async def _fetch(self, symbol):
        from bot.backtest import fetch_history
        return (await fetch_history(self.client, symbol, "15", 200), await fetch_history(self.client, symbol, "60", 100),
                await fetch_history(self.client, symbol, "240", 120))

    async def close(self, now_ms: int) -> dict:
        from bot.ai import forward_collector as fc
        res = await fc.close_window(self.store, self.window["window_id"], self._bars_since, self._funding_since,
                                    exit_policy=self.manifest.exit_policy(), now_ms=now_ms)
        self.closed = True
        await self._refresh()
        self.health.update(observer_running=False)
        if self.log is not None:
            self.log.warning("[FORWARD_WINDOW_CLOSED] %s", res)
        return res

    # ── one boundary ────────────────────────────────────────────────────
    async def run_once(self, now_ms: int | None = None) -> list:
        """Evaluate the current boundary for all 12 symbols (pinned order) and
        journal one row per symbol. At/after window_end_ms: close the window."""
        from bot.ai import evidence_store as es
        from bot.ai import forward_collector as fc
        from bot.ai import forward_evidence as fe
        from bot.nexus_oos_real_replay import _funding_at
        if self.window is None:
            raise ObserverRefused("no committed evidence window; observation refused")
        if self.closed:
            return []
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        b = (now_ms // fe.M15_MS) * fe.M15_MS
        wid = self.window["window_id"]
        try:
            w = await self._refresh()
            if w["status"] != es.W_ACTIVE:
                raise ObserverRefused(f"window is {w['status']}")
            if w.get("continuity_broken"):
                raise es.EvidenceContinuityBroken(f"window continuity broken ({w.get('continuity_reason')})")
            if b >= w["window_end_ms"]:
                await self.detect_gaps(now_ms)
                return [{"event": "FORWARD_WINDOW_CLOSED", **await self.close(now_ms)}]
            if b < w["first_boundary_ms"]:
                return [{"stage": "WAITING_FOR_WINDOW_START", "window_start_ms": w["first_boundary_ms"]}]
            await self.detect_gaps(now_ms)
            w = self.window
            last = w.get("last_completed_boundary_ms")
            if (last is not None and b <= int(last)) or now_ms - b > fe.MAX_DECISION_LATENESS_MS:
                return [{"stage": "BOUNDARY_ALREADY_ACCOUNTED", "boundary_ms": b}]
            t0 = time.perf_counter()
            await self.store.update_window(wid, boundary_in_progress_ms=b)
            out, gaps = [], 0
            for symbol in self.symbols:
                try:
                    k15, k1h, k4h = await self._fetch(symbol)
                    events = await self._funding_since(symbol, b - 3 * 8 * 3_600_000, b)
                    r = await self.evaluate(symbol, k15, k1h, k4h, decision_ts=b, now_ms=now_ms,
                                            funding=_funding_at(events, b))
                except (es.EvidenceContinuityBroken, es.WindowRefused):
                    raise
                except Exception as exc:
                    r = {"symbol": symbol, "stage": "ERROR", "error": type(exc).__name__}
                st = boundary_status(r)
                gaps += st in ("DATA_MISSING", "ERROR")
                await self.store.put_boundary(wid, b, symbol, st, {k: v for k, v in r.items()
                                                                  if k in ("stage", "reason", "detail", "error",
                                                                           "candidate_id", "ai_decision")})
                out.append(r)
            res = await fc.resolve_pending(self.store, wid, self._bars_since, self._funding_since, now_ms=now_ms,
                                           exit_policy=self.manifest.exit_policy())
            await self.store.heartbeat(wid, {"ts": b, "kind": "SCAN", "boundary_ms": b,
                                             "latency_ms": (time.perf_counter() - t0) * 1000.0, "resolver": res,
                                             "gaps": gaps, "safety": self.safety()})
            await self.store.update_window(wid, boundary_in_progress_ms=None, last_completed_boundary_ms=b,
                                           completed_boundaries=int(w.get("completed_boundaries") or 0) + 1)
            await self._refresh()
            self.health.update(last_successful_scan_ms=now_ms, pending_candidates=res["pending"],
                               resolved_candidates=self.health["resolved_candidates"] + res["resolved"],
                               data_gaps=self.health["data_gaps"] + gaps)
            return out
        except es.EvidenceContinuityBroken:
            self.health.update(evidence_continuity_broken=True, observer_running=False)
            raise

    async def persist_outage(self) -> bool:
        """After an in-process DB failure: once the DB answers again, record the
        break durably in the window row (never cleared)."""
        if not getattr(self.store, "broken", False) or self.window is None:
            return False
        reason = self.store.broken_reason or "DB_FAILURE"
        await self.store.mark_broken(self.window["window_id"], reason)
        await self._refresh()
        return True

    def status(self) -> dict:
        """Read-only health snapshot. The observer never reports PASS."""
        broken = bool(self.health.get("evidence_continuity_broken"))
        verdict = "BLOCK" if broken or self.health.get("window_status") == "INVALID_IDENTITY_CHANGE" else \
            "INSUFFICIENT_EVIDENCE" if self.closed else "COLLECTING"
        return {**self.health, **self.safety(), "orders_sent": self.orders_sent, "evidence_verdict": verdict}


SLIPPAGE_MODEL_VERSION = "KUCOIN_EXECUTION_MODEL_SLIPPAGE_V1 (BACKTEST_SLIPPAGE base; 2x for non BTC/ETH/SOL)"


async def main() -> None:           # pragma: no cover - service entry point (not deployed)
    import json
    from bot import nexus_oos_execution_parity as xp
    from bot import nexus_oos_replay_manifest as rm
    from bot.ai.evidence_store import EvidenceContinuityBroken, EvidenceStore
    from bot.logger import log
    from bot.runtime_bootstrap import install as install_runtime
    env = dict(os.environ)
    preflight(env)                                    # 1. no credentials (before any network / DB)
    manifest = rm.load(env.get("SHADOW_POLICY_MANIFEST") or None)
    install_runtime()
    obs = ShadowObserver(env, manifest=manifest, log=log)   # 1-3. no credentials, read-only client, universe
    obs.store = store = await EvidenceStore.connect(env)    # 4-5. PostgreSQL + isolation; refuses otherwise
    obs.start()                                       # 6-10. bundle, policy/schema/metadata, contract,
                                                      #       manifest runtime parity, code provenance
    await obs.open_window(int(time.time() * 1000))    # 11. window created/loaded and committed
    log.warning("[AI_SHADOW_WINDOW] %s", json.dumps({k: obs.window[k] for k in (
        "window_id", "window_start_ms", "window_end_ms", "status", "last_completed_boundary_ms")}, sort_keys=True))
    log.warning("[AI_SHADOW_EVIDENCE_DB] %s", json.dumps(store.identity, sort_keys=True))
    async with obs.client:                            # 12. only now observe the market
        for c in await obs.client._get("/api/v1/contracts/active") or []:
            if isinstance(c, dict) and str(c.get("symbol", "")).endswith("USDTM"):
                base = {"XBT": "BTC"}.get(str(c.get("baseCurrency", "")), str(c.get("baseCurrency", "")))
                info = xp.instrument_from_public_contract(c)
                if "contractMaintainMarginReference" in info:
                    obs.mmr_proxy[f"{base}USDT"] = info["contractMaintainMarginReference"]
        log.warning(obs.observation_line(deployment_id=env.get("RAILWAY_DEPLOYMENT_ID")))
        while not obs.closed:
            now = time.time()
            await asyncio.sleep(900 - (now % 900) + 20)     # 20 s after each 15m boundary
            try:
                for rec in await obs.run_once():
                    log.info("[AI_SHADOW_OBSERVER] %s", rec)
            except EvidenceContinuityBroken as exc:
                log.error("[AI_SHADOW_CONTINUITY_BROKEN] %s — collection halted", exc)
                while True:                                  # persist the break once the DB answers
                    try:
                        await obs.persist_outage()
                        break
                    except Exception:
                        await asyncio.sleep(30)
                return
            log.info("[AI_SHADOW_HEALTH] %s", json.dumps(obs.status(), sort_keys=True, default=str))
    log.warning("[FORWARD_WINDOW_CLOSED] no new window is started automatically")


if __name__ == "__main__":          # pragma: no cover
    asyncio.run(main())
