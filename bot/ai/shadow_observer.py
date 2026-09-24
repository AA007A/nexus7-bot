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
  * it reproduces AI_RUNTIME_HOOK_POPULATION_V1 (LIVE pilot profile) from
    public data with the SAME primitives as the replay: Analyzer.analyze_mtf,
    the scan funnel, nexus_ai.decide, production CROSS geometry with the
    public maintainMargin MMR proxy (APPROXIMATED, as in the replay), NEXUS
    recheck, bot.ai.hook.evaluate_candidate, canonical features, and the
    pinned bundle via AIRuntime (SHADOW) + the durable AI journal.

Run (future isolated service only; never in the production service):
    python -m bot.ai.shadow_observer
"""
from __future__ import annotations

import asyncio
import os
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


class ShadowObserver:
    def __init__(self, env=None, *, client=None, runtime=None, manifest=None, symbols=None, log=None):
        self.env = dict(os.environ if env is None else env)
        preflight(self.env)
        from bot.nexus_oos_real_replay import PublicKuCoinFuturesClient
        self.client = client or PublicKuCoinFuturesClient()
        assert_read_only(self.client)
        self.runtime = runtime
        self.manifest = manifest
        self.symbols = list(symbols or str(self.env.get("SHADOW_SYMBOLS", "BTCUSDT ETHUSDT")).split())
        self.mmr_proxy: dict = {}
        self.log = log
        self.orders_sent = 0            # structurally always 0: there is no sender

    def start(self, candidate_sha=None) -> None:
        from bot.ai import hook as ai_hook
        from bot.ai.runtime import AIRuntime
        if self.runtime is None:
            self.runtime = AIRuntime(self.env, paper_trade=True, log=self.log)
        if self.runtime.mode != "SHADOW":
            raise ObserverRefused("observer runtime must be SHADOW")
        self.runtime.startup(candidate_sha=candidate_sha, hook_profile=ai_hook.PROFILE_LIVE_PILOT)

    async def evaluate(self, symbol: str, k15, k1h, k4h, *, decision_ts: int, now_ms: int,
                       funding=None) -> dict:
        """One symbol at one 15m boundary, exactly as the replay does."""
        from bot import nexus_ai
        from bot import nexus_oos_execution_parity as xp
        from bot.ai import hook as ai_hook
        from bot.backtest import _closed_window_by_ts, _timestamp_index
        from bot.config import cfg
        from bot.engine import TradingEngine
        from bot.kucoin_execution_model import configured_taker_fee, estimated_round_trip_cost_pct
        from bot.nexus_oos_real_replay import _decide, _freeze_nexus_clock, runtime_nexus_threshold
        from bot.strategy import Analyzer
        man = self.manifest
        w15 = _closed_window_by_ts(k15, _timestamp_index(k15), decision_ts, 15, 80)
        w1h = _closed_window_by_ts(k1h, _timestamp_index(k1h), decision_ts, 60, 50)
        w4h = _closed_window_by_ts(k4h, _timestamp_index(k4h), decision_ts, 240, 30)
        if len(w15) < 60 or len(w1h) < 40 or len(w4h) < 20:
            return {"symbol": symbol, "stage": "INSUFFICIENT_DATA"}
        sig = Analyzer().analyze_mtf(symbol, w15, w1h, w4h, min_score=int(man["MIN_ENTRY_SCORE"]),
                                     fee_mult=float(man["FEE_MULTIPLIER"]),
                                     vol_mult=getattr(cfg, "MIN_VOLUME_MULT", 1.2))
        if not sig:
            return {"symbol": symbol, "stage": "NO_SIGNAL"}
        threshold = runtime_nexus_threshold(nexus_ai)
        # Same ticker proxy as the replay: the open of the decision candle.
        forming = [c for c in k15 if (int(c["ts"]) * (1000 if int(c["ts"]) < 10**11 else 1)) == decision_ts]
        ticker = {"lastPrice": str(float((forming[0] if forming else w15[-1])["o" if forming else "c"]))}
        funnel = xp.funnel_flags(sig, decision_ts, TradingEngine)
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
        out = await self.runtime.gate(hk["observation"], w15, w1h, w4h,
                                      taker_fee=float(configured_taker_fee()), now_ms=now_ms)
        return {"symbol": symbol, "stage": "AI_HOOK", "reason": out.reason, "allow": out.allow,
                "decision_id": out.decision.decision_id if out.decision else None}

    async def run_once(self, now_ms: int | None = None) -> list:
        from bot.backtest import fetch_history
        from bot.kucoin_execution_model import fetch_public_funding_history
        from bot.nexus_oos_real_replay import _funding_at
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        decision_ts = (now_ms // 900_000) * 900_000
        out = []
        for symbol in self.symbols:
            try:
                k15 = await fetch_history(self.client, symbol, "15", 200)
                k1h = await fetch_history(self.client, symbol, "60", 100)
                k4h = await fetch_history(self.client, symbol, "240", 120)
                events = await fetch_public_funding_history(self.client, symbol, decision_ts - 3 * 8 * 3_600_000,
                                                            decision_ts)
                out.append(await self.evaluate(symbol, k15, k1h, k4h, decision_ts=decision_ts, now_ms=now_ms,
                                               funding=_funding_at(events, decision_ts)))
            except Exception as exc:
                out.append({"symbol": symbol, "stage": "ERROR", "error": type(exc).__name__})
        return out


async def main() -> None:           # pragma: no cover - service entry point (not deployed)
    from bot import database as db
    from bot import nexus_oos_execution_parity as xp
    from bot import nexus_oos_replay_manifest as rm
    from bot.logger import log
    from bot.runtime_bootstrap import install as install_runtime
    env = dict(os.environ)
    preflight(env)
    manifest = rm.load(env.get("SHADOW_POLICY_MANIFEST") or None)
    install_runtime()
    await db.init()
    obs = ShadowObserver(env, manifest=manifest, log=log)
    async with obs.client:
        for c in await obs.client._get("/api/v1/contracts/active") or []:
            if isinstance(c, dict) and str(c.get("symbol", "")).endswith("USDTM"):
                base = {"XBT": "BTC"}.get(str(c.get("baseCurrency", "")), str(c.get("baseCurrency", "")))
                info = xp.instrument_from_public_contract(c)
                if "contractMaintainMarginReference" in info:
                    obs.mmr_proxy[f"{base}USDT"] = info["contractMaintainMarginReference"]
        obs.start(candidate_sha=env.get("RAILWAY_GIT_COMMIT_SHA"))
        log.warning(obs.runtime.observation_line(candidate_sha=env.get("RAILWAY_GIT_COMMIT_SHA"),
                                                 deployment_id=env.get("RAILWAY_DEPLOYMENT_ID")))
        while True:
            now = time.time()
            await asyncio.sleep(900 - (now % 900) + 20)     # 20 s after each 15m close
            for rec in await obs.run_once():
                log.info("[AI_SHADOW_OBSERVER] %s", rec)


if __name__ == "__main__":          # pragma: no cover
    asyncio.run(main())
