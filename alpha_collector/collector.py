"""Phase 8H collector orchestrator (research-only market-data evidence store).

Modes
  PREFLIGHT : verify DB authority/schema/contract, public reachability, symbol mapping, a short WebSocket
              session, timestamp validation, and write PREFLIGHT_ONLY records; then stay idle (never collects).
  COLLECT   : requires both explicit T0 authorization and an explicit storage-readiness gate; registers / resumes
              PHASE8H_EPOCH_V1 (T0 never moves backward) and collects prospectively.
No trading imports, no credentials, no order capability.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from alpha_collector import book as B
from alpha_collector import contract as C
from alpha_collector import db as D
from alpha_collector import flow as F
from alpha_collector import sources as S

T0_AUTH_PHRASE = "I_AUTHORIZE_PHASE8H_EPOCH_V1_PROSPECTIVE_COLLECTION"
FORBIDDEN_ENV = ("KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_API_PASSPHRASE", "KC_API_KEY", "API_SECRET",
                 "EXECUTION_LEASE", "DATABASE_URL")


def env_guard(env) -> dict:
    """Fail closed on anything that could give this service trading or production authority."""
    present = [k for k in FORBIDDEN_ENV if env.get(k)]
    if present:
        raise D.Refused(f"forbidden environment variables present: {sorted(present)}")
    if str(env.get("PAPER_TRADE", "true")).lower() != "true" or env.get("LIVE_TRADING_CONFIRMED"):
        raise D.Refused("PAPER_TRADE must be true and LIVE_TRADING_CONFIRMED empty")
    exp = env.get("EXPECTED_CONTRACT_SHA256")
    if exp != C.CONTRACT_SHA256:
        raise D.Refused("EXPECTED_CONTRACT_SHA256 does not match the frozen data contract")
    if env.get("DB_AUTHORITY", C.DB_AUTHORITY) != C.DB_AUTHORITY:
        raise D.Refused("unexpected DB_AUTHORITY")
    mode = env.get("COLLECTOR_MODE", "PREFLIGHT")
    if mode not in ("PREFLIGHT", "COLLECT"):
        raise D.Refused("COLLECTOR_MODE must be PREFLIGHT or COLLECT")
    if mode == "COLLECT":
        if env.get("PROSPECTIVE_T0_AUTHORIZATION") != T0_AUTH_PHRASE:
            raise D.Refused("COLLECT mode requires explicit T0 authorization")
        if str(env.get("PROSPECTIVE_STORAGE_READY", "false")).lower() != "true":
            raise D.Refused("COLLECT mode requires PROSPECTIVE_STORAGE_READY=true")
    return {"mode": mode}


class Collector:
    def __init__(self, store, rest, *, mode="PREFLIGHT", code_sha="unknown", instance_id=None, connect=None,
                 sleep=asyncio.sleep, clock=S.now_ms, symbols=C.SYMBOLS):
        self.store, self.rest, self.mode, self.code_sha = store, rest, mode, code_sha
        self.instance_id = instance_id or f"{C.COLLECTOR_VERSION}:{uuid.uuid4().hex[:12]}"
        self.connect, self.sleep, self.clock, self.symbols = connect, sleep, clock, tuple(symbols)
        self.epoch_id = C.PREFLIGHT_EPOCH_ID if mode == "PREFLIGHT" else C.EPOCH_ID
        self.books = {s: B.OrderBook(s) for s in self.symbols}
        self.health = {ch: S.Health() for ch in ("rest", "ws_trades", "ws_book")}
        if isinstance(getattr(rest, "health", None), S.Health):
            self.health["rest"] = rest.health                 # count what the REST client actually does
        self.trade_buffer: list = []
        self.flow_pending: dict = {}                          # (symbol, trade_id) -> record, until its minute closes
        self.flow_emitted_before = 0                          # buckets < this were aggregated (never re-emitted)
        self.trade_uncovered: list = []                       # [start, end) intervals without a live trade stream
        self.trade_connected = False
        self.gaps = 0
        self.last_persisted = None
        self.last_ok = {}
        self.multipliers = {}
        self.down_at = {}
        self.epoch = None
        self.persisted = {t: 0 for t in D.DATA_TABLES}

    # ── startup ──
    async def startup(self, *, t0_ms=None) -> dict:
        await self.store.migrate()
        await self.store.verify_authority()
        self.multipliers = await S.contract_multipliers(self.rest)
        manifest = C.epoch_manifest(code_sha=self.code_sha, db_schema_sha256=D.DB_SCHEMA_SHA256,
                                    multipliers=self.multipliers, t0_ms=t0_ms if t0_ms is not None else 0,
                                    created_at_ms=0, epoch_id=self.epoch_id)
        if self.mode == "COLLECT":
            if t0_ms is None:
                raise D.Refused("COLLECT requires T0")
            self.epoch = await self.store.register_epoch(manifest)
        else:
            self.epoch = manifest
        return {"epoch": self.epoch, "multipliers": self.multipliers}

    # ── persistence helpers ──
    async def _persist(self, table, recs):
        recs = [r for r in recs if r is not None]
        n = await self.store.insert(table, recs)
        self.persisted[table] += n
        if n:
            self.last_persisted = self.clock()
        return n

    async def _gap(self, channel, symbol, start, end, reason):
        self.gaps += 1
        await self.store.gap(channel=channel, symbol=symbol, start_ts=start, end_ts=end, reason=reason, epoch=self.epoch_id)

    # ── pollers ──
    async def poll_once(self):
        """One OI + funding/mark round for every symbol; failures become gaps (never zeros)."""
        for s in self.symbols:
            t = self.clock()
            try:
                oi = await S.poll_open_interest(self.rest, s, instance_id=self.instance_id, epoch_id=self.epoch_id)
                await self._persist("oi_snapshots", oi)
                self.last_ok[("oi", s)] = t
            except S.SourceUnavailable:
                await self._gap("rest:contracts", s, self.last_ok.get(("oi", s), t), t, "SOURCE_UNAVAILABLE")
            try:
                fr, mk = await S.poll_funding_mark(self.rest, s, instance_id=self.instance_id, epoch_id=self.epoch_id)
                await self._persist("funding_live_snapshots", [fr])
                await self._persist("mark_index_snapshots", [mk])
                self.last_ok[("fm", s)] = t
            except S.SourceUnavailable:
                await self._gap("rest:funding-mark", s, self.last_ok.get(("fm", s), t), t, "SOURCE_UNAVAILABLE")

    # ── websocket handlers ──
    async def on_trade(self, msg, obs):
        topic = msg.get("topic", "")
        vs = topic.split(":")[-1]
        sym = next((k for k, v in C.SYMBOL_MAPPING.items() if v == vs), None)
        if sym is None or msg.get("subject") != "match":
            return
        rec = F.trade_record(msg, observed_ts=obs, instance_id=self.instance_id, epoch_id=self.epoch_id,
                             canonical_symbol=sym)
        self.trade_buffer.append(rec)

    async def flush_trades(self, *, final_before_ms=None):
        """Persist raw trades; aggregate a 1m bucket only once it has closed (exactly once, from the full minute).

        Trades whose minute was already aggregated are persisted raw but excluded from flow and counted as
        ``late_trades``; buckets overlapping a period without a live trade stream are flagged INCOMPLETE_BUCKET.
        """
        buf, self.trade_buffer = self.trade_buffer, []
        n = await self._persist("trade_events", buf) if buf else 0
        for r in buf:
            if r["event_ts"] is None or r["payload"].get("taker_side_venue") is None:
                continue
            if r["event_ts"] < self.flow_emitted_before:
                self.health["ws_trades"].inc("late_trades")
                continue
            self.flow_pending.setdefault((r["symbol"], r["venue_trade_id"]), r)
        if final_before_ms is not None and final_before_ms > self.flow_emitted_before:
            ready = [r for r in self.flow_pending.values() if r["event_ts"] < final_before_ms]
            for r in ready:
                del self.flow_pending[(r["symbol"], r["venue_trade_id"])]
            agg = F.aggregate(ready, self.multipliers)
            incomplete = {k for k in agg if self._bucket_uncovered(k[1])}
            await self._persist("trade_flow_1m", F.flow_records(agg, observed_ts=self.clock(), instance_id=self.instance_id,
                                                                epoch_id=self.epoch_id, incomplete=incomplete))
            self.flow_emitted_before = final_before_ms
        return n

    def _bucket_uncovered(self, b):
        end = b + F.BUCKET_MS
        open_down = [(self.down_at["ws:execution"], end)] if "ws:execution" in self.down_at else []
        return any(s < end and e > b for s, e in self.trade_uncovered + open_down)

    async def on_book(self, msg, obs):
        vs = msg.get("topic", "").split(":")[-1]
        sym = next((k for k, v in C.SYMBOL_MAPPING.items() if v == vs), None)
        if sym is None:
            return
        d = msg.get("data") or {}
        try:
            price, side, size = str(d["change"]).split(",")
        except (KeyError, ValueError, TypeError):
            self.health["ws_book"].inc("parse_failures")
            return
        res = self.books[sym].on_delta(int(d["sequence"]), price, side, size, d.get("timestamp"))
        if res == "GAP":
            self.health["ws_book"].inc("sequence_gaps")
            await self._gap("ws:level2", sym, obs, None, "BOOK_SEQUENCE_GAP")
            await self.resync(sym)

    async def resync(self, sym):
        bk = self.books[sym]
        bk.begin_sync()
        bo = S.Backoff()
        for delay in C.BOOK_POLICY["resync_backoff_s"]:
            try:
                seq, bids, asks, ts = await S.book_snapshot(self.rest, sym)
                if bk.on_snapshot(seq, bids, asks, ts) == B.VALID:
                    self.health["ws_book"].inc("resyncs")
                    return True
                bk.begin_sync()
            except S.SourceUnavailable:
                pass
            await self.sleep(min(delay, bo.next()))
        bk.invalidate(self.clock(), "RESYNC_FAILED")
        await self._gap("ws:level2", sym, self.clock(), None, "BOOK_RESYNC_FAILED")
        return False

    async def book_snapshot_records(self):
        recs = []
        obs = self.clock()
        for s in self.symbols:
            bk = self.books[s]
            f = bk.features()
            if f is None:
                continue
            r = C.make_record(symbol=s, metric="book_features", event_ts=F.ns_to_ms(bk.last_event_ts)
                              if bk.last_event_ts else None, observed_ts=obs, value=f["mid"], units="quote_usdt",
                              source_channel="ws:level2+rest:snapshot", raw=f, instance_id=self.instance_id,
                              epoch_id=self.epoch_id, sequence=bk.seq, payload=f)
            r["book_state"] = bk.state
            recs.append(r)
        return await self._persist("book_feature_snapshots", recs)

    async def on_ws_down(self, channel, _since):
        self.down_at[channel] = self.clock()
        if channel == "ws:level2":
            for s in self.symbols:
                self.books[s].invalidate(self.clock(), "DISCONNECT")

    async def on_ws_up(self, channel):
        t = self.clock()
        if channel == "ws:execution":
            if not self.trade_connected:
                self.trade_uncovered.append((0, t))           # the minute in which the stream first joined is partial
                self.trade_connected = True
            elif channel in self.down_at:
                self.trade_uncovered.append((self.down_at[channel], t))
        if channel in self.down_at:
            for s in self.symbols:
                await self._gap(channel, s, self.down_at[channel], t, "WS_DISCONNECT")
            del self.down_at[channel]
        if channel == "ws:level2":
            for s in self.symbols:
                await self.resync(s)

    # ── heartbeat / health / sealing ──
    async def heartbeat(self, health="OK"):
        hb = {"collector": C.COLLECTOR_VERSION, "instance_id": self.instance_id, "ts": self.clock(),
              "last_source_event_ts": max((h.c.get("last_event_ts") or 0) for h in self.health.values()) or None,
              "last_persisted_ts": self.last_persisted, "health": health,
              "reconnects": sum(h.c["ws_reconnects"] for h in self.health.values()), "gaps": self.gaps,
              "epoch": self.epoch_id, "detail": {"mode": self.mode, "persisted": self.persisted,
                                                "book_states": {s: b.state for s, b in self.books.items()}}}
        await self.store.heartbeat(hb)
        # Sanitized operational observability only. Never include URLs, tokens,
        # credentials, raw payloads, DB connection strings, or authorization values.
        safe_keys = ("requests", "successes", "rate_limited", "retries", "parse_failures",
                     "ws_reconnects", "sequence_gaps", "resyncs", "last_event_ts",
                     "max_lag_ms", "clock_skew_ms", "session_errors")
        source_health = {name: {k: h.c.get(k) for k in safe_keys if k in h.c}
                         for name, h in self.health.items()}
        book_state_counts = {}
        for b in self.books.values():
            book_state_counts[b.state] = book_state_counts.get(b.state, 0) + 1
        log_record = {"event": "ALPHA_COLLECTOR_HEARTBEAT", "collector": C.COLLECTOR_VERSION,
                      "instance_id": self.instance_id, "ts": hb["ts"], "health": health,
                      "mode": self.mode, "epoch": self.epoch_id,
                      "last_source_event_ts": hb["last_source_event_ts"],
                      "last_persisted_ts": hb["last_persisted_ts"],
                      "reconnects": hb["reconnects"], "gaps": hb["gaps"],
                      "persisted": dict(self.persisted), "book_state_counts": book_state_counts,
                      "source_health": source_health}
        print(json.dumps(log_record, sort_keys=True, separators=(",", ":")), flush=True)
        return hb

    async def seal_completed_days(self, days):
        out = []
        for day in days:
            for t in D.DATA_TABLES:
                out.append(await self.store.seal_day(t, day, self.epoch_id))
        return out

    def ws_topics(self):
        syms = ",".join(C.SYMBOL_MAPPING[s] for s in self.symbols)
        return [f"/contractMarket/execution:{syms}"], [f"/contractMarket/level2:{syms}"]

    # ── main loop (COLLECT) ──
    async def run(self, *, stop_after_s=None):
        trade_topics, book_topics = self.ws_topics()
        tws = S.WsSession(self.rest, trade_topics, self.on_trade, connect=self.connect, health=self.health["ws_trades"],
                          on_disconnect=lambda s: self.on_ws_down("ws:execution", s),
                          on_connect=lambda: self.on_ws_up("ws:execution"))
        for s in self.symbols:
            self.books[s].begin_sync()
        bws = S.WsSession(self.rest, book_topics, self.on_book, connect=self.connect, health=self.health["ws_book"],
                          on_disconnect=lambda s: self.on_ws_down("ws:level2", s),
                          on_connect=lambda: self._book_connected())
        tasks = [asyncio.create_task(tws.run()), asyncio.create_task(bws.run()), asyncio.create_task(self._ticker())]
        t_end = time.time() + stop_after_s if stop_after_s else None
        try:
            while t_end is None or time.time() < t_end:
                await asyncio.sleep(1.0)
        finally:
            tws.stop = bws.stop = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.flush_trades(final_before_ms=self.clock() // 60_000 * 60_000)

    async def _book_connected(self):
        for s in self.symbols:
            await asyncio.sleep(0.2)
            await self.resync(s)

    async def _ticker(self):
        t_poll = t_book = t_hb = t_flush = 0.0
        while True:
            now = time.time()
            if now - t_poll >= C.SAMPLING_POLICY["open_interest_poll_s"]:
                t_poll = now
                await self.poll_once()
            if now - t_book >= C.SAMPLING_POLICY["book_feature_snapshot_s"]:
                t_book = now
                await self.book_snapshot_records()
            if now - t_flush >= 5:
                t_flush = now
                await self.flush_trades(final_before_ms=(self.clock() - 5_000) // 60_000 * 60_000)
            if now - t_hb >= C.SAMPLING_POLICY["heartbeat_s"]:
                t_hb = now
                await self.heartbeat()
            await asyncio.sleep(0.5)


async def main_async(env=os.environ) -> int:
    g = env_guard(env)
    store = await D.PgStore.connect(env.get("PROSPECTIVE_DB_URL", ""), production_fingerprint=env.get("PRODUCTION_DB_FINGERPRINT"))
    rest = S.RestClient()
    try:
        col = Collector(store, rest, mode=g["mode"], code_sha=env.get("RAILWAY_GIT_COMMIT_SHA", "unknown"))
        if g["mode"] == "PREFLIGHT":
            await col.startup()
            await col.poll_once()
            await col.run(stop_after_s=int(env.get("PREFLIGHT_WS_SECONDS", "60")))      # brief WS validation only
            await col.heartbeat("PREFLIGHT_OK")
            while True:                                       # PREFLIGHT never transitions into collection
                await asyncio.sleep(300)
                await col.heartbeat("PREFLIGHT_IDLE")
        t0 = int(env["PROSPECTIVE_T0_MS"])
        await col.startup(t0_ms=t0)
        await col.run()
    finally:
        await rest.close()
        await store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))