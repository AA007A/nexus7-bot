"""Phase 8H: prospective alpha data collector (no trading capability, no outcomes)."""
import asyncio
import copy
import importlib
import os
import pathlib
import random
import sys
import unittest

from alpha_collector import book as B
from alpha_collector import collector as K
from alpha_collector import contract as C
from alpha_collector import db as D
from alpha_collector import flow as F
from alpha_collector import sources as S

PG = os.environ.get("TEST_PG_DSN")
PKG = pathlib.Path(__file__).resolve().parents[1] / "alpha_collector"


def run(coro):
    return asyncio.run(coro)


def rec(ts=1_790_000_000_000, sym="BTCUSDT", value="123", epoch=C.EPOCH_ID, bucket=None, **kw):
    r = C.make_record(symbol=sym, metric="open_interest_lots", event_ts=kw.pop("event_ts", ts), observed_ts=ts, value=value,
                      units="lots", source_channel="rest:contracts", raw={"v": value, "ts": ts}, instance_id="i1",
                      epoch_id=epoch, **kw)
    r["bucket_ts"] = bucket if bucket is not None else ts // 60000 * 60000
    return r


def trade(i, ts, side="buy", price="100.5", size="3", sym="XBTUSDTM"):
    return {"type": "message", "topic": f"/contractMarket/execution:{sym}", "subject": "match",
            "data": {"symbol": sym, "sequence": i, "side": side, "size": size, "price": price, "tradeId": f"t{i}",
                     "ts": ts * 1_000_000}}


class FakeRest:
    def __init__(self, fail=False):
        self.fail, self.snap_seq = fail, 100

    async def call(self, path, params=None, method="GET"):
        if self.fail:
            raise S.SourceUnavailable(path)
        if path == "/api/v1/contracts/active":
            return [{"symbol": v, "multiplier": "0.001", "status": "Open"} for v in C.SYMBOL_MAPPING.values()]
        if path.startswith("/api/v1/contracts/"):
            return {"openInterest": "5000", "multiplier": "0.001"}
        if path.startswith("/api/v1/funding-rate/"):
            return {"value": 0.0001, "predictedValue": 0.00012, "timePoint": S.now_ms() - 1000, "granularity": 28800000}
        if path.startswith("/api/v1/mark-price/"):
            return {"value": 100.0, "indexPrice": 100.1, "timePoint": S.now_ms() - 1000}
        if path == "/api/v1/level2/snapshot":
            return {"sequence": self.snap_seq, "bids": [["99", "5"], ["98", "7"]], "asks": [["101", "4"], ["102", "6"]],
                    "ts": S.now_ms() * 1_000_000}
        if path == "/api/v1/timestamp":
            return S.now_ms()
        if path == "/api/v1/bullet-public":
            return {"token": "PUBLIC_TOKEN", "instanceServers": [{"endpoint": "wss://x", "pingInterval": 18000}]}
        raise AssertionError(path)


async def nosleep(_):
    return None


class Contract(unittest.TestCase):
    def test_01_contract_hash_deterministic(self):
        importlib.reload(C)
        a = C.CONTRACT_SHA256
        importlib.reload(C)
        self.assertEqual(a, C.CONTRACT_SHA256)
        self.assertEqual(len(a), 64)
        self.assertEqual(C.sha(copy.deepcopy(C.CONTRACT)), a)

    def test_02_epoch_manifest_deterministic(self):
        kw = {"code_sha": "c" * 40, "db_schema_sha256": D.DB_SCHEMA_SHA256, "multipliers": {"BTCUSDT": "0.001"},
              "t0_ms": 5, "created_at_ms": 0}
        self.assertEqual(C.epoch_manifest(**kw)["manifest_sha256"], C.epoch_manifest(**kw)["manifest_sha256"])
        self.assertNotEqual(C.epoch_manifest(**kw)["manifest_sha256"], C.epoch_manifest(**dict(kw, t0_ms=6))["manifest_sha256"])

    def test_03_three_timestamps_distinct(self):
        r = rec(ts=1_000, event_ts=900)
        r2 = C.check_ingest(r, 1_500)
        self.assertEqual((r2["event_ts"], r2["observed_ts"], r2["ingested_ts"]), (900, 1_000, 1_500))
        oi = rec(ts=2_000, event_ts=None)
        self.assertIn("EVENT_TS_FROM_OBSERVATION", oi["quality_flags"])

    def test_04_future_timestamp_flagged(self):
        r = rec(ts=1_000_000, event_ts=1_000_000 + C.CLOCK_TOLERANCE_MS + 1)
        self.assertIn("FUTURE_EVENT_TS", r["quality_flags"])
        self.assertIn("CLOCK_ORDER_VIOLATION", C.check_ingest(rec(ts=10_000), 10_000 - C.CLOCK_TOLERANCE_MS - 1)["quality_flags"])

    def test_05_missing_never_zero(self):
        for v in (None, "", "n/a"):
            r = rec(value=v)
            self.assertIsNone(r["value"])
            self.assertIn("MISSING", r["quality_flags"])

    def test_23_no_outcome_fields(self):
        text = (D.DDL + " " + " ".join(C.CANONICAL_FIELDS)).lower()
        for bad in ("pnl", "profit", "future_return", "label", "target", "prediction", "outcome", "r_multiple"):
            self.assertNotIn(bad, text)


class Idempotency(unittest.TestCase):
    def test_06_oi_duplicate_idempotent(self):
        st = D.MemoryStore()
        self.assertEqual(run(st.insert("oi_snapshots", [rec(), rec()])), 1)
        self.assertEqual(run(st.insert("oi_snapshots", [rec()])), 0)

    def test_07_trade_id_duplicate_idempotent(self):
        st = D.MemoryStore()
        t = F.trade_record(trade(1, 1_790_000_000_000), observed_ts=1_790_000_000_100, instance_id="i", epoch_id=C.EPOCH_ID,
                           canonical_symbol="BTCUSDT")
        self.assertEqual(run(st.insert("trade_events", [t, dict(t, observed_ts=t["observed_ts"] + 5)])), 1)

    def test_08_trade_aggregation_exact(self):
        base = 1_790_000_000_000 // 60000 * 60000
        rng = random.Random(3)
        trades = [F.trade_record(trade(i, base + rng.randint(0, 179_999), side=rng.choice(["buy", "sell"]),
                                       price=f"{100 + rng.random():.4f}", size=str(rng.randint(1, 50))),
                                 observed_ts=base + 200_000, instance_id="i", epoch_id=C.EPOCH_ID, canonical_symbol="BTCUSDT")
                  for i in range(500)]
        a = F.aggregate(trades, {"BTCUSDT": "0.001"})
        b = F.aggregate(list(reversed(copy.deepcopy(trades))), {"BTCUSDT": "0.001"})
        self.assertEqual(a, b)
        self.assertEqual(sum(v["total_count"] for v in a.values()), 500)
        from decimal import Decimal
        tot = sum(Decimal(t["payload"]["price"]) * Decimal(t["payload"]["size"]) * Decimal("0.001") for t in trades)
        self.assertEqual(sum(Decimal(v["total_notional"]) for v in a.values()), tot)


class Book(unittest.TestCase):
    def _valid(self):
        bk = B.OrderBook("BTCUSDT")
        bk.begin_sync()
        bk.on_delta(101, "99", "buy", "6")
        bk.on_snapshot(100, [["99", "5"]], [["101", "4"]])
        return bk

    def test_09_sequence_gap_detected(self):
        bk = self._valid()
        self.assertEqual(bk.state, B.VALID)
        self.assertEqual(bk.on_delta(102, "98", "buy", "1"), "APPLIED")
        self.assertEqual(bk.on_delta(105, "98", "buy", "2"), "GAP")
        self.assertEqual(bk.state, B.INVALID)
        self.assertIsNone(bk.features())

    def test_10_forced_resync(self):
        col = K.Collector(D.MemoryStore(), FakeRest(), mode="PREFLIGHT", sleep=nosleep, symbols=("BTCUSDT",))
        bk = col.books["BTCUSDT"]
        bk.begin_sync()
        bk.on_snapshot(100, [["99", "5"]], [["101", "4"]])
        msg = {"topic": "/contractMarket/level2:XBTUSDTM", "data": {"sequence": 150, "change": "99,buy,1", "timestamp": 1}}
        run(col.on_book(msg, S.now_ms()))
        self.assertEqual(bk.state, B.VALID)                       # resynced from REST snapshot
        self.assertEqual(bk.stats["gaps"], 1)
        self.assertEqual(len(col.store.gaps), 1)

    def test_11_out_of_order_and_duplicates(self):
        bk = self._valid()
        self.assertEqual(bk.on_delta(101, "99", "buy", "9"), "DUPLICATE")
        self.assertEqual(bk.on_delta(90, "99", "buy", "9"), "DUPLICATE")
        self.assertEqual(bk.state, B.VALID)
        self.assertEqual(bk.features()["bid1_size"], "6")          # buffered delta 101 applied once


class FakeWs:
    def __init__(self, msgs):
        self.msgs, self.sent = list(msgs), []

    async def send(self, m):
        self.sent.append(m)

    async def recv(self):
        if not self.msgs:
            raise ConnectionError("closed")
        import json
        return json.dumps(self.msgs.pop(0))

    async def close(self):
        pass


class Network(unittest.TestCase):
    def test_12_reconnect_handling(self):
        state = {"n": 0}
        got = []

        async def connect(url):
            state["n"] += 1
            if state["n"] == 1:
                raise ConnectionError("refused")
            return FakeWs([trade(state["n"], S.now_ms())])

        async def on_msg(m, o):
            got.append(m)
        downs, ups = [], []

        async def down(s):
            downs.append(s)

        async def up():
            ups.append(1)
        ws = S.WsSession(FakeRest(), ["/t"], on_msg, connect=connect, sleep=nosleep, on_disconnect=down, on_connect=up)
        run(ws.run(max_sessions=3))
        self.assertEqual(ws.health.c["ws_reconnects"], 3)
        self.assertEqual(len(got), 2)
        self.assertEqual(len(ups), 2)

    def test_13_rate_limit_backoff(self):
        calls, sleeps = [], []

        async def transport(method, path, params):
            calls.append(path)
            if len(calls) <= 2:
                return 429, "{}", {"retry-after": "2"}
            return 200, '{"code":"200000","data":123}', {}

        async def sl(s):
            sleeps.append(s)
        rc = S.RestClient(transport, sleep=sl, rng=random.Random(1))
        self.assertEqual(run(rc.call("/api/v1/timestamp")), 123)
        self.assertEqual(rc.health.c["rate_limited"], 2)
        self.assertTrue(all(2.0 <= s <= C.RETRY_POLICY["max_s"] for s in sleeps))
        bo = S.Backoff(rng=random.Random(0))
        self.assertTrue(all(bo.next() <= C.RETRY_POLICY["max_s"] for _ in range(20)))
        with self.assertRaises(PermissionError):
            run(rc.call("/api/v1/orders"))

    def test_25_source_outage_persists_gap(self):
        st = D.MemoryStore()
        col = K.Collector(st, FakeRest(fail=True), mode="PREFLIGHT", sleep=nosleep, symbols=("BTCUSDT", "ETHUSDT"))
        run(col.poll_once())
        self.assertEqual(sum(len(v) for v in st.tables.values()), 0)
        self.assertEqual(len(st.gaps), 4)


class Authority(unittest.TestCase):
    def _env(self, **kw):
        e = {"EXPECTED_CONTRACT_SHA256": C.CONTRACT_SHA256, "PAPER_TRADE": "true", "COLLECTOR_MODE": "PREFLIGHT"}
        e.update(kw)
        return e

    def test_15_wrong_db_authority_fails_closed(self):
        st = D.MemoryStore()
        with self.assertRaises(D.Refused):
            run(st.verify_authority())
        run(st.init_authority("SOME_OTHER_AUTHORITY"))
        with self.assertRaises(D.Refused):
            run(K.Collector(st, FakeRest()).startup())
        with self.assertRaises(D.Refused):
            K.env_guard(self._env(DB_AUTHORITY="PRODUCTION"))

    def test_16_wrong_contract_hash_fails_closed(self):
        with self.assertRaises(D.Refused):
            K.env_guard(self._env(EXPECTED_CONTRACT_SHA256="0" * 64))
        st = D.MemoryStore()
        run(st.init_authority())
        st.authority["contract_sha256"] = "f" * 64
        with self.assertRaises(D.Refused):
            run(st.verify_authority())

    def test_21_no_private_credentials(self):
        with self.assertRaises(D.Refused):
            K.env_guard(self._env(KUCOIN_API_KEY="x"))
        with self.assertRaises(D.Refused):
            K.env_guard(self._env(COLLECTOR_MODE="COLLECT"))            # no T0 authorization
        self.assertEqual(K.env_guard(self._env())["mode"], "PREFLIGHT")

    def test_22_no_production_db_reference(self):
        with self.assertRaises(D.Refused):
            K.env_guard(self._env(DATABASE_URL="postgresql://prod"))
        url = "postgresql://u:p@research-db:5432/r"
        with self.assertRaises(D.Refused):
            D.check_not_production(url, D.url_fingerprint(url))
        src = "".join(p.read_text() for p in PKG.glob("*.py"))
        self.assertNotIn("os.environ.get(\"DATABASE_URL\"", src)


class Safety(unittest.TestCase):
    def test_20_no_execution_client(self):
        src = "".join(p.read_text() for p in PKG.glob("*.py"))
        for bad in ("from bot", "import bot", "KC-API-SIGN", "KC-API-KEY", "hmac", "/api/v1/orders", "/api/v1/st-orders",
                    "place_order", "create_order", "acquire_lease", "LeaseManager"):
            self.assertNotIn(bad, src)
        before = {m for m in sys.modules if m == "bot" or m.startswith("bot.")}
        for mod in ("contract", "db", "book", "flow", "sources", "collector", "preflight"):
            importlib.import_module(f"alpha_collector.{mod}")
        after = {m for m in sys.modules if m == "bot" or m.startswith("bot.")}
        self.assertEqual(after - before, set())

    def test_24_preflight_excluded_from_epoch(self):
        st = D.MemoryStore()
        run(st.insert("oi_snapshots", [rec(epoch=C.PREFLIGHT_EPOCH_ID), rec(ts=1_790_000_060_000)]))
        self.assertEqual(run(st.count("oi_snapshots")), 1)
        self.assertEqual(run(st.count("oi_snapshots", include_preflight=True)), 2)
        self.assertIn("PREFLIGHT_ONLY", rec(epoch=C.PREFLIGHT_EPOCH_ID)["quality_flags"])
        col = K.Collector(st, FakeRest(), mode="PREFLIGHT")
        self.assertEqual(col.epoch_id, C.PREFLIGHT_EPOCH_ID)


class Sealing(unittest.TestCase):
    def test_17_sealed_partition_cannot_be_rewritten(self):
        st = D.MemoryStore()
        run(st.insert("oi_snapshots", [rec()]))
        run(st.seal_day("oi_snapshots", D.utc_day(1_790_000_000_000), C.EPOCH_ID))
        with self.assertRaises(D.Refused):
            run(st.insert("oi_snapshots", [rec(bucket=1)]))
        with self.assertRaises(D.Refused):
            st.update("oi_snapshots", None)

    def test_18_manifest_changes_with_data(self):
        a = D.manifest_of("oi_snapshots", "2026-09-21", C.EPOCH_ID, [rec()], [])
        b = D.manifest_of("oi_snapshots", "2026-09-21", C.EPOCH_ID, [rec(value="124")], [])
        self.assertNotEqual(a["digest"], b["digest"])
        self.assertEqual(a["digest"], D.manifest_of("oi_snapshots", "2026-09-21", C.EPOCH_ID, [rec()], [])["digest"])


@unittest.skipUnless(PG, "TEST_PG_DSN not set (disposable Postgres)")
class Postgres(unittest.TestCase):
    async def _store(self):
        st = await D.PgStore.connect(PG)
        await st.c.execute(f"DROP SCHEMA IF EXISTS {C.DB_SCHEMA} CASCADE")
        await st.migrate()
        return st

    def test_pg_migration_idempotency_seal_and_authority(self):
        async def go():
            st = await self._store()
            await st.migrate()                                               # idempotent migration
            with self.assertRaises(D.Refused):
                await st.verify_authority()
            await st.init_authority()
            self.assertEqual(await st.insert("oi_snapshots", [rec(), rec()]), 1)
            self.assertEqual(await st.insert("oi_snapshots", [rec()]), 0)
            import asyncpg
            with self.assertRaises(asyncpg.exceptions.RaiseError):
                await st.c.execute(f"UPDATE {C.DB_SCHEMA}.oi_snapshots SET value = 0")
            with self.assertRaises(asyncpg.exceptions.RaiseError):
                await st.c.execute(f"DELETE FROM {C.DB_SCHEMA}.oi_snapshots")
            m = await st.seal_day("oi_snapshots", D.utc_day(1_790_000_000_000), C.EPOCH_ID)
            self.assertEqual(m["rows"], 1)
            with self.assertRaises(D.Refused):
                await st.insert("oi_snapshots", [rec(bucket=5)])
            rows = await st.rows("oi_snapshots")
            self.assertIsNotNone(rows[0]["ingested_ts"])
            await st.close()
        run(go())

    def test_14_19_restart_recovery_and_heartbeat_durability(self):
        async def go():
            st = await self._store()
            await st.init_authority()
            col = K.Collector(st, FakeRest(), mode="COLLECT", sleep=nosleep, symbols=("BTCUSDT",))
            await col.startup(t0_ms=1_790_100_000_000)
            await col.poll_once()
            hb = await col.heartbeat()
            await st.close()
            st2 = await D.PgStore.connect(PG)                                # restart
            await st2.migrate()
            col2 = K.Collector(st2, FakeRest(), mode="COLLECT", sleep=nosleep, symbols=("BTCUSDT",))
            await col2.startup(t0_ms=1_790_100_000_000)                       # same epoch resumes
            with self.assertRaises(D.DuplicateEpoch):
                await K.Collector(st2, FakeRest(), mode="COLLECT").startup(t0_ms=1_790_000_000_000)   # T0 cannot move
            n = await st2.c.fetchval(f"SELECT count(*) FROM {C.DB_SCHEMA}.collector_heartbeats WHERE ts = $1", hb["ts"])
            self.assertEqual(n, 1)
            await st2.close()
        run(go())


class RestartMemory(unittest.TestCase):
    def test_14_restart_recovery_memory(self):
        st = D.MemoryStore()
        run(st.init_authority())
        c1 = K.Collector(st, FakeRest(), mode="COLLECT", sleep=nosleep, symbols=("BTCUSDT",))
        run(c1.startup(t0_ms=1))
        run(c1.poll_once())
        c2 = K.Collector(st, FakeRest(), mode="COLLECT", sleep=nosleep, symbols=("BTCUSDT",))
        run(c2.startup(t0_ms=1))
        with self.assertRaises(D.DuplicateEpoch):
            run(K.Collector(st, FakeRest(), mode="COLLECT").startup(t0_ms=0))

    def test_19_heartbeat_written(self):
        st = D.MemoryStore()
        col = K.Collector(st, FakeRest(), mode="PREFLIGHT", sleep=nosleep, symbols=("BTCUSDT",))
        hb = run(col.heartbeat())
        self.assertEqual(st.heartbeats[-1]["ts"], hb["ts"])
        self.assertEqual(hb["epoch"], C.PREFLIGHT_EPOCH_ID)


class FlowAcrossFlushes(unittest.TestCase):
    """Regression: a minute's trades arrive over many 5 s flushes; the bucket must be aggregated once, after it closes."""

    def _col(self, now):
        col = K.Collector(D.MemoryStore(), FakeRest(), mode="PREFLIGHT", sleep=nosleep, symbols=("BTCUSDT",),
                          clock=lambda: now[0])
        col.multipliers = {"BTCUSDT": "0.001"}
        return col

    def test_bucket_emitted_once_after_close(self):
        m0 = 1_790_000_040_000 // 60000 * 60000
        now = [m0 - 30_000]
        col = self._col(now)
        run(col.on_ws_up("ws:execution"))                          # joined mid-way through the minute before m0
        for i, off in enumerate([1_000, 20_000, 40_000, 59_000]):
            now[0] = m0 + off
            run(col.on_trade(trade(i, m0 + off, side="buy" if i % 2 else "sell"), now[0]))
            run(col.on_trade(trade(i, m0 + off), now[0]))           # duplicate delivery of the same tradeId
            run(col.flush_trades(final_before_ms=(now[0] - 5_000) // 60000 * 60000))
        self.assertEqual(col.persisted["trade_flow_1m"], 0)        # minute m0 still open
        now[0] = m0 + 66_000
        run(col.flush_trades(final_before_ms=(now[0] - 5_000) // 60000 * 60000))
        flows = list(col.store.tables["trade_flow_1m"].values())
        self.assertEqual(len(flows), 1)
        f = flows[0]
        self.assertEqual(f["bucket_ts"], m0)
        self.assertEqual(f["payload"]["total_count"], 4)          # 4 distinct trades, duplicates ignored
        self.assertEqual(f["payload"]["buy_count"], 2)
        self.assertNotIn("INCOMPLETE_BUCKET", f["quality_flags"])  # stream covered the whole minute
        # a trade for an already-aggregated minute is kept raw but never re-aggregated
        run(col.on_trade(trade(99, m0 + 10_000), now[0]))
        run(col.flush_trades(final_before_ms=m0 + 60_000))
        self.assertEqual(len(list(col.store.tables["trade_flow_1m"].values())), 1)
        self.assertEqual(col.health["ws_trades"].c["late_trades"], 1)

    def test_partial_minute_flagged_incomplete(self):
        m0 = 1_790_000_040_000 // 60000 * 60000
        now = [m0 + 30_000]
        col = self._col(now)
        run(col.on_ws_up("ws:execution"))                          # joined at m0+30s -> minute m0 is partial
        run(col.on_trade(trade(1, m0 + 31_000), now[0]))
        now[0] = m0 + 61_000
        run(col.on_trade(trade(2, m0 + 60_500), now[0]))
        now[0] = m0 + 126_000
        run(col.flush_trades(final_before_ms=(now[0] - 5_000) // 60000 * 60000))
        flags = {r["bucket_ts"]: r["quality_flags"] for r in list(col.store.tables["trade_flow_1m"].values())}
        self.assertIn("INCOMPLETE_BUCKET", flags[m0])
        self.assertNotIn("INCOMPLETE_BUCKET", flags[m0 + 60_000])

    def test_rest_health_wired(self):
        rest = S.RestClient(transport=lambda *a: _ok(), sleep=nosleep)
        col = K.Collector(D.MemoryStore(), rest, mode="PREFLIGHT", sleep=nosleep, symbols=("BTCUSDT",))
        run(rest.call("/api/v1/timestamp"))
        self.assertIs(col.health["rest"], rest.health)
        self.assertEqual(col.health["rest"].c["successes"], 1)


async def _ok():
    return 200, '{"code": "200000", "data": 1}', {}


if __name__ == "__main__":
    unittest.main()
