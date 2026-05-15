"""
Bybit V5 REST API Client
Signature: HMAC-SHA256(timestamp + apiKey + recvWindow + payload)
"""
import hmac, hashlib, time, json, asyncio
from urllib.parse import urlencode
from typing import Optional, List
import aiohttp

from bot.config import cfg
from bot.logger import log

BASE = "https://api.bybit.com"
RW   = "5000"   # recvWindow — must match in both signature and header

INTERVALS = {
    "1m":"1","3m":"3","5m":"5","15m":"15","30m":"30",
    "1h":"60","2h":"120","4h":"240","1d":"D",
}


def _sign(secret: str, ts: str, key: str, payload: str) -> str:
    raw = ts + key + RW + payload
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _headers(key: str, secret: str, payload: str) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "X-BAPI-API-KEY":     key,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        _sign(secret, ts, key, payload),
        "X-BAPI-RECV-WINDOW": RW,
        "Content-Type":       "application/json",
    }


class BybitClient:
    def __init__(self):
        self.key     = cfg.API_KEY
        self.secret  = cfg.API_SECRET
        self._sess: Optional[aiohttp.ClientSession] = None
        log.info(f"🔑 Bybit key: {self.key[:8]}... ({len(self.key)} chars)")

    async def _s(self) -> aiohttp.ClientSession:
        if not self._sess or self._sess.closed:
            self._sess = aiohttp.ClientSession()
        return self._sess

    async def _get(self, path: str, params: dict = None, auth: bool = False) -> dict:
        qs  = urlencode(sorted((params or {}).items()))
        url = f"{BASE}{path}?{qs}" if qs else f"{BASE}{path}"
        hdrs = _headers(self.key, self.secret, qs) if auth else {}
        s = await self._s()
        async with s.get(url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=10)) as r:
            raw = await r.text()
            try:
                data = json.loads(raw)
            except Exception:
                raise Exception(f"Non-JSON: {raw[:150]}")
            rc = data.get("retCode", 0)
            if rc != 0:
                raise Exception(f"Bybit {rc}: {data.get('retMsg','')}")
            return data.get("result", {})

    async def _post(self, path: str, body: dict = None) -> dict:
        bstr = json.dumps(body or {}, separators=(',', ':'))
        hdrs = _headers(self.key, self.secret, bstr)
        s = await self._s()
        async with s.post(f"{BASE}{path}", headers=hdrs, data=bstr,
                          timeout=aiohttp.ClientTimeout(total=10)) as r:
            raw = await r.text()
            try:
                data = json.loads(raw)
            except Exception:
                raise Exception(f"Non-JSON: {raw[:150]}")
            rc = data.get("retCode", 0)
            if rc != 0:
                raise Exception(f"Bybit {rc}: {data.get('retMsg','')}")
            return data.get("result", {})

    # ── Public ────────────────────────────────────────────────
    async def ping(self) -> bool:
        try:
            await self._get("/v5/market/time")
            return True
        except Exception:
            return False

    async def get_klines(self, symbol: str, interval: str = "5m",
                         limit: int = 100) -> List[dict]:
        data = await self._get("/v5/market/kline", {
            "category": "linear",
            "symbol":   symbol,
            "interval": INTERVALS.get(interval, "5"),
            "limit":    str(limit),
        })
        raw = list(reversed(data.get("list", [])))
        return [{"t": r[0], "o": float(r[1]), "h": float(r[2]),
                 "l": float(r[3]), "c": float(r[4]), "v": float(r[5])} for r in raw]

    async def get_ticker(self, symbol: str) -> dict:
        data = await self._get("/v5/market/tickers",
                               {"category": "linear", "symbol": symbol})
        lst = data.get("list", [{}])
        return lst[0] if lst else {}

    # ── Private ───────────────────────────────────────────────
    async def get_balance(self) -> float:
        try:
            data = await self._get("/v5/account/wallet-balance",
                                   {"accountType": "UNIFIED"}, auth=True)
            for item in data.get("list", []):
                for coin in item.get("coin", []):
                    if coin.get("coin") == "USDT":
                        val = float(coin.get("walletBalance", 0))
                        log.info(f"💰 Saldo: ${val:.2f} USDT")
                        return val
            return 0.0
        except Exception as e:
            log.error(f"get_balance: {e}")
            return -1.0

    async def get_positions(self, symbol: str = None) -> list:
        try:
            params = {"category": "linear"}
            if symbol:
                params["symbol"] = symbol
            data = await self._get("/v5/position/list", params, auth=True)
            return data.get("list", [])
        except Exception as e:
            log.error(f"get_positions: {e}")
            return []

    async def place_order(self, symbol: str, side: str, qty: float,
                          sl: float = None, tp: float = None) -> dict:
        body = {
            "category":    "linear",
            "symbol":      symbol,
            "side":        side,        # "Buy" or "Sell"
            "orderType":   "Market",
            "qty":         str(round(qty, 3)),
            "timeInForce": "GoodTillCancel",
        }
        if sl: body["stopLoss"]   = str(round(sl, 2))
        if tp: body["takeProfit"] = str(round(tp, 2))
        log.info(f"📤 {side} {qty} {symbol} SL={sl:.0f} TP={tp:.0f}")
        return await self._post("/v5/order/create", body)

    async def close_position(self, symbol: str, side: str, qty: float) -> dict:
        close_side = "Sell" if side == "Buy" else "Buy"
        return await self._post("/v5/order/create", {
            "category":    "linear",
            "symbol":      symbol,
            "side":        close_side,
            "orderType":   "Market",
            "qty":         str(round(qty, 3)),
            "timeInForce": "GoodTillCancel",
            "reduceOnly":  True,
        })

    async def cancel_all(self, symbol: str = None) -> dict:
        try:
            body = {"category": "linear"}
            if symbol:
                body["symbol"] = symbol
            return await self._post("/v5/order/cancel-all", body)
        except Exception as e:
            log.warning(f"cancel_all: {e}")
            return {}

    async def set_leverage(self, symbol: str, lev: int):
        try:
            await self._post("/v5/position/set-leverage", {
                "category": "linear", "symbol": symbol,
                "buyLeverage": str(lev), "sellLeverage": str(lev),
            })
            log.info(f"⚙️ Leverage {lev}x — {symbol}")
        except Exception as e:
            if "leverage not modified" not in str(e).lower():
                log.warning(f"set_leverage {symbol}: {e}")

    async def update_stop_loss(self, symbol: str, sl: float) -> dict:
        """Atualiza o stop loss de uma posição aberta via Trading Stop."""
        try:
            result = await self._post("/v5/position/trading-stop", {
                "category":  "linear",
                "symbol":    symbol,
                "stopLoss":  str(round(sl, 6)),
                "slTriggerBy": "MarkPrice",
            })
            log.info(f"🔒 SL atualizado {symbol} → ${sl:.4f}")
            return result
        except Exception as e:
            log.error(f"update_stop_loss {symbol}: {e}")
            return {}

    async def get_closed_pnl(self, symbol: str = None,
                             start_ms: int = None, end_ms: int = None,
                             limit: int = 200) -> list:
        """Retorna PnL realizado de posições fechadas."""
        try:
            params: dict = {"category": "linear", "limit": str(limit)}
            if symbol:
                params["symbol"] = symbol
            if start_ms:
                params["startTime"] = str(start_ms)
            if end_ms:
                params["endTime"] = str(end_ms)
            data = await self._get("/v5/position/closed-pnl", params, auth=True)
            return data.get("list", [])
        except Exception as e:
            log.error(f"get_closed_pnl: {e}")
            return []

    async def close(self):
        if self._sess and not self._sess.closed:
            await self._sess.close()
