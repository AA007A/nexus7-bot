"""KuCoin Futures PUBLIC adapters: REST (GET + public bullet token) and WebSocket.

No credentials, no signing, no order/account endpoints. Transport/connect
functions are injectable for deterministic tests.
"""
from __future__ import annotations

import asyncio
import json
import random
import time

from alpha_collector import contract as C

BASE = C.SOURCE_ENDPOINTS["rest_base"]
ALLOWED_PATH_PREFIXES = ("/api/v1/timestamp", "/api/v1/contracts/", "/api/v1/funding-rate/", "/api/v1/mark-price/",
                         "/api/v1/level2/snapshot", "/api/v1/bullet-public")


def now_ms() -> int:
    return int(time.time() * 1000)


class Backoff:
    def __init__(self, *, base=C.RETRY_POLICY["base_s"], factor=C.RETRY_POLICY["factor"], cap=C.RETRY_POLICY["max_s"],
                 rng=None):
        self.base, self.factor, self.cap = base, factor, cap
        self.rng = rng or random.Random()
        self.attempt = 0

    def next(self, hint_s: float | None = None) -> float:
        ceiling = min(self.cap, self.base * self.factor ** self.attempt)
        self.attempt += 1
        d = self.rng.uniform(0, ceiling)
        return max(d, min(self.cap, hint_s)) if hint_s else d

    def reset(self):
        self.attempt = 0


class Health:
    def __init__(self):
        self.c = {"requests": 0, "successes": 0, "rate_limited": 0, "retries": 0, "parse_failures": 0,
                  "ws_reconnects": 0, "sequence_gaps": 0, "resyncs": 0, "last_event_ts": None,
                  "last_write_ts": None, "max_lag_ms": 0, "clock_skew_ms": None}

    def inc(self, k, n=1):
        self.c[k] = (self.c.get(k) or 0) + n


class RestClient:
    """GET-only public client (plus the public bullet POST). Refuses any other path."""

    def __init__(self, transport=None, *, sleep=asyncio.sleep, rng=None, max_attempts=5, health=None):
        self.transport = transport
        self.sleep, self.rng, self.max_attempts = sleep, rng or random.Random(), max_attempts
        self.health = health or Health()
        self._session = None

    async def _default_transport(self, method, path, params):
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        fn = self._session.post if method == "POST" else self._session.get
        async with fn(BASE + path, params=params or {}) as r:
            body = await r.text()
            hdr = {k.lower(): v for k, v in r.headers.items()}
            return r.status, body, hdr

    async def close(self):
        if self._session is not None:
            await self._session.close()

    async def call(self, path: str, params=None, *, method="GET"):
        if not path.startswith(ALLOWED_PATH_PREFIXES) or (method == "POST" and path != "/api/v1/bullet-public"):
            raise PermissionError(f"path not allowed for a public research collector: {method} {path}")
        bo = Backoff(rng=self.rng)
        last = None
        for attempt in range(self.max_attempts):
            self.health.inc("requests")
            try:
                status, body, hdr = await (self.transport or self._default_transport)(method, path, params)
            except Exception as exc:
                last = type(exc).__name__
                status, body, hdr = None, None, {}
            if status == 200:
                try:
                    js = json.loads(body)
                except Exception:
                    self.health.inc("parse_failures")
                    last = "PARSE"
                    js = None
                if isinstance(js, dict) and str(js.get("code")) == "200000":
                    self.health.inc("successes")
                    return js.get("data")
                last = last or f"API_CODE:{(js or {}).get('code') if isinstance(js, dict) else 'n/a'}"
            hint = None
            if status == 429:
                self.health.inc("rate_limited")
                ra = hdr.get("retry-after") or hdr.get("gw-ratelimit-reset")
                try:
                    hint = float(ra) / (1000.0 if ra and float(ra) > 1000 else 1.0)
                except (TypeError, ValueError):
                    hint = None
                last = "RATE_LIMITED"
            elif status is not None and status != 200:
                last = f"HTTP_{status}"
            if attempt + 1 < self.max_attempts:
                self.health.inc("retries")
                await self.sleep(bo.next(hint))
        raise SourceUnavailable(f"{path}: {last}")


class SourceUnavailable(RuntimeError):
    pass


# ── REST snapshots -> canonical records ────────────────────────────────────
def _bucket(ts, s):
    return int(ts) // (s * 1000) * (s * 1000)


async def poll_open_interest(rest, sym, *, instance_id, epoch_id, mark=None):
    vs = C.SYMBOL_MAPPING[sym]
    t_req = now_ms()
    d = await rest.call(f"/api/v1/contracts/{vs}")
    obs = now_ms()
    lots = d.get("openInterest") if isinstance(d, dict) else None
    mult = d.get("multiplier") if isinstance(d, dict) else None
    recs = []
    base = C.make_record(symbol=sym, metric="open_interest_lots", event_ts=None, observed_ts=obs, value=lots,
                         units="lots", source_channel="rest:contracts", raw=d, instance_id=instance_id, epoch_id=epoch_id,
                         payload={"multiplier": None if mult is None else str(mult), "request_ts": t_req,
                                  "venue_symbol": vs})
    base["bucket_ts"] = _bucket(obs, C.SAMPLING_POLICY["open_interest_poll_s"])
    recs.append(base)
    return recs


async def poll_funding_mark(rest, sym, *, instance_id, epoch_id):
    vs = C.SYMBOL_MAPPING[sym]
    f = await rest.call(f"/api/v1/funding-rate/{vs}/current")
    of = now_ms()
    m = await rest.call(f"/api/v1/mark-price/{vs}/current")
    om = now_ms()
    b = _bucket(of, C.SAMPLING_POLICY["funding_mark_poll_s"])
    fr = C.make_record(symbol=sym, metric="funding_rate_predicted", event_ts=None, observed_ts=of,
                       value=(f or {}).get("predictedValue"), units="rate_per_interval", source_channel="rest:funding-rate",
                       raw=f, instance_id=instance_id, epoch_id=epoch_id,
                       payload={"current_value": None if (f or {}).get("value") is None else str(f["value"]),
                                "timePoint": (f or {}).get("timePoint"), "granularity": (f or {}).get("granularity"),
                                "field_predicted": "predictedValue", "field_current": "value"})
    fr["bucket_ts"] = b
    mk = C.make_record(symbol=sym, metric="mark_price", event_ts=(m or {}).get("timePoint"), observed_ts=om,
                       value=(m or {}).get("value"), units="quote_usdt", source_channel="rest:mark-price", raw=m,
                       instance_id=instance_id, epoch_id=epoch_id,
                       payload={"index_price": None if (m or {}).get("indexPrice") is None else str(m["indexPrice"]),
                                "timePoint": (m or {}).get("timePoint"), "granularity": (m or {}).get("granularity")})
    mk["bucket_ts"] = _bucket(om, C.SAMPLING_POLICY["funding_mark_poll_s"])
    return fr, mk


async def server_skew(rest) -> dict:
    t0 = now_ms()
    srv = await rest.call("/api/v1/timestamp")
    t1 = now_ms()
    mid = (t0 + t1) // 2
    return {"server_ms": int(srv), "local_mid_ms": mid, "skew_ms": mid - int(srv), "rtt_ms": t1 - t0,
            "alarm": abs(mid - int(srv)) > C.SKEW_ALARM_MS}


async def contract_multipliers(rest) -> dict:
    data = await rest.call("/api/v1/contracts/active")
    by = {c.get("symbol"): c for c in data or [] if isinstance(c, dict)}
    out, missing = {}, []
    for sym, vs in C.SYMBOL_MAPPING.items():
        c = by.get(vs)
        if not c or c.get("status") not in (None, "Open") or c.get("multiplier") is None:
            missing.append(vs)
        else:
            out[sym] = str(c["multiplier"])
    if missing:
        raise SourceUnavailable(f"unexpected symbol mapping: {missing}")
    return out


async def book_snapshot(rest, sym):
    d = await rest.call("/api/v1/level2/snapshot", {"symbol": C.SYMBOL_MAPPING[sym]})
    return int(d["sequence"]), d.get("bids") or [], d.get("asks") or [], d.get("ts")


# ── WebSocket ──────────────────────────────────────────────────────────────
async def ws_endpoint(rest) -> tuple[str, float]:
    """Public bullet token -> connect URL. The token is used in memory only."""
    d = await rest.call("/api/v1/bullet-public", method="POST")
    srv = d["instanceServers"][0]
    return f"{srv['endpoint']}?token={d['token']}&connectId={random.randint(1, 10 ** 12)}", srv.get("pingInterval", 18000) / 1000


class WsSession:
    """Resilient subscription loop. ``connect`` returns an object with async send/recv/close."""

    def __init__(self, rest, topics, on_message, *, connect=None, sleep=asyncio.sleep, health=None, on_disconnect=None,
                 on_connect=None, rng=None, idle_timeout_s=60.0):
        self.rest, self.topics, self.on_message = rest, list(topics), on_message
        self.connect, self.sleep = connect, sleep
        self.health = health or Health()
        self.on_disconnect, self.on_connect = on_disconnect, on_connect
        self.backoff = Backoff(rng=rng)
        self.idle_timeout_s = idle_timeout_s
        self.stop = False

    async def _default_connect(self, url):
        import websockets
        return await websockets.connect(url, max_size=2 ** 22, ping_interval=None)

    async def run(self, *, max_sessions=None):
        sessions = 0
        while not self.stop and (max_sessions is None or sessions < max_sessions):
            sessions += 1
            ws = None
            down_since = now_ms()
            try:
                url, ping_s = await ws_endpoint(self.rest)
                ws = await (self.connect or self._default_connect)(url)
                for i, t in enumerate(self.topics):
                    await ws.send(json.dumps({"id": str(i + 1), "type": "subscribe", "topic": t, "privateChannel": False,
                                              "response": True}))
                if self.on_connect:
                    await self.on_connect()
                self.backoff.reset()
                last_ping = time.time()
                while not self.stop:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout_s)
                    if raw is None:
                        break
                    obs = now_ms()
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        self.health.inc("parse_failures")
                        continue
                    if msg.get("type") == "message":
                        self.health.c["last_event_ts"] = obs
                        await self.on_message(msg, obs)
                    if time.time() - last_ping > ping_s * 0.8:
                        await ws.send(json.dumps({"id": str(int(time.time())), "type": "ping"}))
                        last_ping = time.time()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
            if self.stop:
                break
            self.health.inc("ws_reconnects")
            if self.on_disconnect:
                await self.on_disconnect(down_since)
            await self.sleep(self.backoff.next())
