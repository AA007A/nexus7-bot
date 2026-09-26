"""Binance USD-M Futures client for NEXUS-7.

The public interface mirrors the existing KuCoin client so the strategy/risk
engine can switch venues at one boundary. Binance quantities are base-asset
quantities, so instrument metadata uses multiplier=1 and position rows are
explicitly labelled sizeUnit=BASE_ASSET.

Safety:
- PAPER is the default unless the existing double LIVE acknowledgement passes.
- real Binance mutation additionally requires BINANCE_LIVE_MIGRATION_READY=true.
- existing execution-capability, durable ownership and runtime-readiness gates
  remain authoritative for new-risk orders.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from typing import Optional
from urllib.parse import urlencode

import aiohttp
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from bot.execution_capability import assert_exchange_mutation_allowed
from bot.logger import log
from bot.order_state import OrderState, InvalidTransition


def _clean_credential_env(name: str) -> str:
    raw = os.environ.get(name, "")
    cleaned = raw.strip().replace("\r", "").replace("\n", "").replace("\t", "")
    if cleaned != raw.strip():
        log.warning(
            "[BINANCE_CREDENTIAL_SANITIZE] name=%s removed_control_chars=true "
            "credential_value_redacted=true execution_effect=NONE",
            name,
        )
    return cleaned


def _read_pem_env(name: str) -> str:
    raw = os.environ.get(name, "").strip()
    # Railway variables are often pasted as a single line with literal \\n.
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")
    return raw


API_KEY = _clean_credential_env("BINANCE_API_KEY")
API_SECRET = _clean_credential_env("BINANCE_API_SECRET")
SIGNING_METHOD = os.environ.get("BINANCE_SIGNING_METHOD", "hmac").strip().lower()
ED25519_PRIVATE_KEY_B64 = _clean_credential_env("BINANCE_ED25519_PRIVATE_KEY_B64")
ED25519_PRIVATE_KEY_PEM = _read_pem_env("BINANCE_ED25519_PRIVATE_KEY")
_ED25519_PRIVATE_KEY_CACHE: Ed25519PrivateKey | None = None


def _load_ed25519_private_key() -> Ed25519PrivateKey:
    global _ED25519_PRIVATE_KEY_CACHE
    if _ED25519_PRIVATE_KEY_CACHE is not None:
        return _ED25519_PRIVATE_KEY_CACHE

    if ED25519_PRIVATE_KEY_B64:
        try:
            key_bytes = base64.b64decode(
                ED25519_PRIVATE_KEY_B64.encode("ascii"), validate=True
            )
        except Exception as exc:
            raise RuntimeError(
                "BINANCE_ED25519_PRIVATE_KEY_INVALID_BASE64"
            ) from exc
    elif ED25519_PRIVATE_KEY_PEM:
        key_bytes = ED25519_PRIVATE_KEY_PEM.encode("utf-8")
    else:
        raise RuntimeError("BINANCE_ED25519_PRIVATE_KEY_UNAVAILABLE")

    try:
        key = load_pem_private_key(key_bytes, password=None)
    except Exception as exc:
        raise RuntimeError("BINANCE_ED25519_PRIVATE_KEY_INVALID") from exc

    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("BINANCE_ED25519_PRIVATE_KEY_NOT_ED25519")

    _ED25519_PRIVATE_KEY_CACHE = key
    return key


def _sign_payload(payload: str) -> str:
    if SIGNING_METHOD == "hmac":
        if not API_SECRET:
            raise RuntimeError("BINANCE_API_SECRET_UNAVAILABLE")
        return hmac.new(
            API_SECRET.encode(), payload.encode("ascii"), hashlib.sha256
        ).hexdigest()

    if SIGNING_METHOD == "ed25519":
        private_key = _load_ed25519_private_key()
        signature = private_key.sign(payload.encode("ascii"))
        return base64.b64encode(signature).decode("ascii")

    raise RuntimeError("BINANCE_SIGNING_METHOD_INVALID")


_paper_env = os.environ.get("PAPER_TRADE", "").strip().lower()
_live_ack = os.environ.get("LIVE_TRADING_CONFIRMED", "").strip()
_LIVE_TOKEN = "I_UNDERSTAND_THE_RISK"

if _paper_env == "false" and _live_ack == _LIVE_TOKEN:
    PAPER_TRADE = False
    TRADING_MODE_REASON = (
        "OPERAÇÃO REAL — confirmada por PAPER_TRADE=false + LIVE_TRADING_CONFIRMED"
    )
elif _paper_env == "false":
    PAPER_TRADE = True
    TRADING_MODE_REASON = "PAPER — PAPER_TRADE=false mas falta LIVE_TRADING_CONFIRMED"
elif _paper_env == "true":
    PAPER_TRADE = True
    TRADING_MODE_REASON = "PAPER — PAPER_TRADE=true (simulação pedida)"
else:
    PAPER_TRADE = True
    TRADING_MODE_REASON = (
        f"PAPER — PAPER_TRADE ausente ou inválido ('{_paper_env}')"
    )

REST_BASE = os.environ.get(
    "BINANCE_FAPI_REST_BASE", "https://fapi.binance.com"
).rstrip("/")
WS_BASE = os.environ.get(
    "BINANCE_FAPI_WS_BASE", "wss://fstream.binance.com"
).rstrip("/")

TAKER_FEE = float(os.environ.get("TAKER_FEE", "0.0005"))
MAKER_FEE = float(os.environ.get("MAKER_FEE", "0.0002"))

INTERVAL_MAP = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "2h",
    "240": "4h",
    "360": "6h",
    "480": "8h",
    "720": "12h",
    "D": "1d",
    "W": "1w",
}
INTERVAL_MINUTES = {
    "1": 1,
    "3": 3,
    "5": 5,
    "15": 15,
    "30": 30,
    "60": 60,
    "120": 120,
    "240": 240,
    "360": 360,
    "480": 480,
    "720": 720,
    "D": 1440,
    "W": 10080,
}
WS_INTERVAL_REV = {v: k for k, v in INTERVAL_MAP.items()}


def to_binance(symbol: str) -> str:
    return str(symbol or "").upper()


def to_standard(symbol: str) -> str:
    return str(symbol or "").upper()


def _truthy(value) -> bool:
    return str(value or "").strip().lower() == "true"


def _live_migration_ready() -> bool:
    return _truthy(os.environ.get("BINANCE_LIVE_MIGRATION_READY", "false"))


def _d(value) -> Decimal:
    return Decimal(str(value))


def _fmt_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class BinanceClient:
    exchange_name = "binance"
    product_name = "USD-M Futures"
    STALE_MULTIPLIER = 3

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._time_offset_ms = 0
        self._time_synced = False
        self._instruments: dict = {}
        self._kline_cache: dict = {}
        self._ticker_cache: dict = {}
        self._ob_cache: dict = {}
        self._stale_logged: dict = {}
        self._ws_tasks: set = set()
        self._private_ws_task = None
        self._listen_key = ""
        self._rate_lock = asyncio.Lock()
        self._rate_sem = asyncio.Semaphore(8)
        self._last_request_ts = 0.0
        self._rate_limit_hits = 0
        self._order_registry = None
        self._order_id_symbol: dict[str, str] = {}
        self._client_oid_symbol: dict[str, str] = {}
        self.entries_paused = False

        if PAPER_TRADE:
            log.warning("🟡 PAPER TRADE — Binance USD-M ordens simuladas")
        else:
            log.warning(
                "🔴 BINANCE LIVE configurado — migration gate ainda é obrigatório"
            )
        if API_KEY:
            log.info("🔑 Binance API Key configurada (valor redigido)")
        else:
            log.warning("⚠️ BINANCE_API_KEY não configurado")

        log.info(
            "[BINANCE_AUTH] signing_method=%s key_material_redacted=true "
            "execution_effect=NONE",
            SIGNING_METHOD,
        )
        if SIGNING_METHOD == "hmac" and API_KEY and not API_SECRET:
            log.error("❌ BINANCE_API_SECRET vazio para signing_method=hmac")
        elif SIGNING_METHOD == "ed25519" and API_KEY and not (
            ED25519_PRIVATE_KEY_B64 or ED25519_PRIVATE_KEY_PEM
        ):
            log.error(
                "❌ Binance Ed25519 selecionado sem private key configurada"
            )
        elif SIGNING_METHOD not in {"hmac", "ed25519"}:
            log.error(
                "❌ BINANCE_SIGNING_METHOD inválido: %s (esperado hmac|ed25519)",
                SIGNING_METHOD,
            )

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def _throttle(self):
        async with self._rate_lock:
            now = time.monotonic()
            wait = 0.04 - (now - self._last_request_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_ts = time.monotonic()

    def _signed_params(self, params: dict | None = None) -> dict:
        if not API_KEY:
            raise RuntimeError("BINANCE_API_KEY_UNAVAILABLE")
        out = dict(params or {})
        out.setdefault(
            "recvWindow", int(os.environ.get("BINANCE_RECV_WINDOW", "5000"))
        )
        out["timestamp"] = self._now_ms()
        query = urlencode(out, doseq=True)
        out["signature"] = _sign_payload(query)
        return out

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        *,
        auth: bool = False,
        mutation: bool = False,
        single_attempt: bool = False,
    ):
        await self._ensure_session()
        method = method.upper()
        if mutation:
            assert_exchange_mutation_allowed(method, endpoint)
            if not PAPER_TRADE and not _live_migration_ready():
                raise RuntimeError(
                    "BINANCE_LIVE_MIGRATION_NOT_RELEASED: "
                    "BINANCE_LIVE_MIGRATION_READY=true required"
                )

        attempts = 1 if single_attempt else 3
        last_error = None
        for attempt in range(attempts):
            req_params = dict(params or {})
            headers = {}
            if auth:
                req_params = self._signed_params(req_params)
                headers["X-MBX-APIKEY"] = API_KEY
            elif API_KEY and endpoint == "/fapi/v1/listenKey":
                headers["X-MBX-APIKEY"] = API_KEY

            await self._throttle()
            try:
                async with self._rate_sem:
                    async with self._session.request(
                        method,
                        REST_BASE + endpoint,
                        params=req_params,
                        headers=headers,
                    ) as resp:
                        raw = await resp.text()
                        try:
                            data = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            data = {"_raw": raw[:300]}

                        if resp.status in (418, 429):
                            self._rate_limit_hits += 1
                            retry = resp.headers.get("Retry-After")
                            try:
                                wait = min(30.0, max(0.25, float(retry)))
                            except (TypeError, ValueError):
                                wait = min(8.0, 2 ** attempt)
                            log.warning(
                                "🚦 Binance rate limit HTTP %s em %s %s — %.1fs",
                                resp.status,
                                method,
                                endpoint,
                                wait,
                            )
                            if attempt + 1 < attempts:
                                await asyncio.sleep(wait)
                                continue
                            raise RuntimeError(
                                f"BINANCE_RATE_LIMIT_{resp.status}"
                            )

                        if 200 <= resp.status < 300:
                            return data

                        code = data.get("code") if isinstance(data, dict) else None
                        msg = data.get("msg", "") if isinstance(data, dict) else ""
                        if code == -1021 and attempt + 1 < attempts:
                            await self.sync_time()
                            continue
                        if resp.status >= 500 and attempt + 1 < attempts:
                            await asyncio.sleep(min(4.0, 2 ** attempt))
                            continue
                        raise RuntimeError(
                            f"Binance {method} {endpoint} HTTP {resp.status} "
                            f"code={code} msg={str(msg)[:180]}"
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(4.0, 2 ** attempt))
                    continue
                raise RuntimeError(
                    f"Binance {method} {endpoint} network failure"
                ) from exc
        if last_error:
            raise last_error
        return {}

    async def _get(self, endpoint: str, params=None, auth: bool = False):
        return await self._request("GET", endpoint, params, auth=auth)

    async def _delete(self, endpoint: str, params=None, auth: bool = True):
        return await self._request(
            "DELETE", endpoint, params, auth=auth, mutation=True
        )

    @asynccontextmanager
    async def _entry_safe_post(self, endpoint, body, url=None, **kwargs):
        if (
            getattr(self, "entries_paused", False)
            and endpoint in {"/fapi/v1/order", "/fapi/v1/algoOrder"}
            and not _truthy(body.get("reduceOnly"))
            and not _truthy(body.get("closePosition"))
        ):
            raise ValueError("New entry blocked by operator pause")

        class _Response:
            status = 200
            headers = {}

            async def json(self, content_type=None):
                return {}

        yield _Response()

    def _is_new_risk_request(self, endpoint: str, body: dict) -> bool:
        if endpoint not in {"/fapi/v1/order", "/fapi/v1/algoOrder"}:
            return False
        return not _truthy(body.get("reduceOnly")) and not _truthy(
            body.get("closePosition")
        )

    @asynccontextmanager
    async def _fenced_entry_post(self, endpoint, body, url=None, **kwargs):
        if self._is_new_risk_request(endpoint, body):
            from bot.execution_ownership import validate_execution_ownership

            ownership = getattr(self, "_execution_ownership", None)
            if ownership is None:
                raise RuntimeError(
                    "OPEN_NEW_RISK missing execution ownership at transport boundary"
                )
            await validate_execution_ownership(ownership)
        async with self._entry_safe_post(endpoint, body, url, **kwargs) as response:
            yield response

    async def _post(
        self, endpoint: str, body: dict, *, single_attempt: bool = False
    ) -> dict:
        return await self._request(
            "POST",
            endpoint,
            body,
            auth=True,
            mutation=True,
            single_attempt=single_attempt,
        )

    def rate_limit_status(self) -> dict:
        return {"recent_hits": self._rate_limit_hits, "exchange": "binance"}

    async def get_balance(self) -> float:
        rows = await self._get("/fapi/v3/balance", auth=True)
        if not isinstance(rows, list):
            raise RuntimeError("Futures balance unavailable")
        row = next(
            (
                item
                for item in rows
                if str(item.get("asset", "")).upper() == "USDT"
            ),
            None,
        )
        if not isinstance(row, dict):
            raise RuntimeError("USDT Futures balance unavailable")
        value = row.get("availableBalance", row.get("balance"))
        try:
            balance = float(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Futures availableBalance invalid") from exc
        if not math.isfinite(balance) or balance < 0:
            raise RuntimeError("Futures availableBalance invalid")
        log.info("💰 Binance Futures available USDT: $%.4f", balance)
        return balance

    async def get_account_state(self) -> dict:
        data = await self._get("/fapi/v3/account", auth=True)
        if not isinstance(data, dict):
            raise RuntimeError("Binance account state unavailable")

        def finite(name, fallback=None):
            raw = data.get(name, fallback)
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError(name)
            return value

        equity = finite(
            "totalMarginBalance", data.get("totalWalletBalance", 0)
        )
        available = finite("availableBalance", 0)
        if equity < 0 or available < 0:
            raise RuntimeError("negative account state")
        return {
            "equity": equity,
            "available": available,
            "available_source": "availableBalance",
            "currency": "USDT",
            "accountEquity": equity,
            "marginBalance": equity,
            "availableBalance": available,
            "availableMargin": available,
            "unrealisedPNL": finite("totalUnrealizedProfit", 0),
            "positionMargin": finite("totalPositionInitialMargin", 0),
            "orderMargin": finite("totalOpenOrderInitialMargin", 0),
            "frozenFunds": finite("totalOpenOrderInitialMargin", 0),
        }

    async def load_instruments(self):
        if not self._time_synced:
            await self.sync_time()
        data = await self._get("/fapi/v1/exchangeInfo")
        symbols = data.get("symbols", []) if isinstance(data, dict) else []

        from bot.config import cfg

        wanted = set(cfg.SYMBOLS)
        loaded = {}
        for item in symbols:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            if symbol not in wanted:
                continue
            if item.get("contractType") != "PERPETUAL":
                continue
            if str(item.get("quoteAsset", "")).upper() != "USDT":
                continue
            if str(item.get("status", "")).upper() != "TRADING":
                continue
            filters = {
                str(f.get("filterType")): f
                for f in item.get("filters", [])
                if isinstance(f, dict)
            }
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("MARKET_LOT_SIZE") or filters.get(
                "LOT_SIZE", {}
            )
            notional_filter = filters.get("MIN_NOTIONAL") or filters.get(
                "NOTIONAL", {}
            )
            tick = float(price_filter.get("tickSize", 0) or 0)
            step = float(lot_filter.get("stepSize", 0) or 0)
            min_qty = float(lot_filter.get("minQty", 0) or 0)
            if tick <= 0 or step <= 0 or min_qty <= 0:
                continue
            min_notional = float(
                notional_filter.get(
                    "notional", notional_filter.get("minNotional", 0)
                )
                or 0
            )
            loaded[symbol] = {
                "minQty": min_qty,
                "lotSize": min_qty,
                "qtyStep": step,
                "tickSize": tick,
                "multiplier": 1.0,
                "minBaseQty": min_qty,
                "minNotional": min_notional,
                "maxLeverage": 0.0,
                "binanceSymbol": symbol,
                "quantityUnit": "BASE_ASSET",
            }

        self._instruments = loaded
        missing = [s for s in cfg.SYMBOLS if s not in loaded]
        log.info(
            "📋 Binance USD-M instrumentos: %d/%d carregados",
            len(loaded),
            len(cfg.SYMBOLS),
        )
        if missing:
            log.warning(
                "⚠️ Binance símbolos indisponíveis: %s", ", ".join(missing)
            )
        return self._instruments

    def get_instruments(self) -> dict:
        return self._instruments

    async def get_symbol_config(self, symbol: str):
        data = await self._get(
            "/fapi/v1/symbolConfig",
            {"symbol": to_binance(symbol)},
            auth=True,
        )
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    async def set_leverage(self, symbol: str, leverage: int):
        if PAPER_TRADE:
            log.info(
                "[PAPER] Binance set_leverage skipped: %s %sx",
                symbol,
                leverage,
            )
            return None
        if not _live_migration_ready():
            raise RuntimeError("BINANCE_LIVE_MIGRATION_NOT_RELEASED")
        return await self._post(
            "/fapi/v1/leverage",
            {"symbol": to_binance(symbol), "leverage": int(leverage)},
            single_attempt=True,
        )

    def _round_price(self, price: float, symbol: str) -> str:
        info = self._instruments.get(symbol)
        if not isinstance(info, dict):
            raise ValueError(f"instrument unavailable: {symbol}")
        tick = _d(info.get("tickSize"))
        if tick <= 0:
            raise ValueError("invalid tickSize")
        try:
            units = (_d(price) / tick).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("invalid price") from exc
        return _fmt_decimal(units * tick)

    def _round_qty(self, qty: float, symbol: str) -> str:
        info = self._instruments.get(symbol)
        if not isinstance(info, dict):
            raise ValueError(f"instrument unavailable: {symbol}")
        step = _d(info.get("qtyStep"))
        minimum = _d(info.get("minQty"))
        if step <= 0 or minimum <= 0:
            raise ValueError("invalid quantity filters")
        try:
            units = (_d(qty) / step).to_integral_value(rounding=ROUND_DOWN)
            rounded = units * step
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("invalid quantity") from exc
        if rounded < minimum:
            raise ValueError(
                f"quantity below Binance minimum: {rounded} < {minimum}"
            )
        return _fmt_decimal(rounded)

    def build_client_oid(
        self,
        symbol: str,
        side: str,
        qty: float,
        idem_key: str = None,
        contracts=None,
    ) -> str:
        window = int(time.time() // 60)
        raw = idem_key or f"{symbol}_{side}_{qty}_{window}"
        oid = "bgx7-" + hashlib.sha256(raw.encode()).hexdigest()[:30]
        self._client_oid_symbol[oid] = to_binance(symbol)
        return oid

    async def get_position_mode(self) -> str:
        data = await self._get("/fapi/v1/positionSide/dual", auth=True)
        return "HEDGE" if bool(data.get("dualSidePosition")) else "ONE_WAY"

    async def _assert_live_account_mode(self, symbol: str):
        if PAPER_TRADE:
            return
        if not _live_migration_ready():
            raise RuntimeError("BINANCE_LIVE_MIGRATION_NOT_RELEASED")
        if await self.get_position_mode() != "ONE_WAY":
            raise RuntimeError("BINANCE_HEDGE_MODE_UNSUPPORTED")
        config = await self.get_symbol_config(symbol)
        margin_type = str(config.get("marginType", "")).upper()
        if margin_type and margin_type not in {"CROSSED", "CROSS"}:
            raise RuntimeError(
                f"BINANCE_MARGIN_MODE_UNSUPPORTED:{margin_type}"
            )

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        sl: float = 0,
        tp: float = 0,
        instruments: dict = None,
        reduce_only: bool = False,
        idem_key: str = None,
        single_submission: bool = False,
    ) -> dict:
        oid = self.build_client_oid(symbol, side, qty, idem_key)
        if PAPER_TRADE:
            log.info(
                "[PAPER][BINANCE] %s %s %s SL=%s TP=%s",
                side,
                qty,
                symbol,
                sl,
                tp,
            )
            return {
                "orderId": f"paper_{int(time.time()*1000)}",
                "clientOid": oid,
            }

        if not API_KEY or not API_SECRET:
            raise RuntimeError("BINANCE_CREDENTIALS_UNAVAILABLE")
        await self._assert_live_account_mode(symbol)

        qty_text = self._round_qty(qty, symbol)
        side_upper = "BUY" if str(side).lower() == "buy" else "SELL"
        params = {
            "symbol": to_binance(symbol),
            "side": side_upper,
            "type": "MARKET",
            "quantity": qty_text,
            "newClientOrderId": oid,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"

        registry = getattr(self, "_order_registry", None)
        managed = None
        if registry is not None:
            managed, _ = registry.get_or_create(
                oid, symbol, side, float(qty)
            )
            managed.reduce_only = bool(reduce_only)
            managed.exposure_intent = (
                "REDUCE" if reduce_only else "INCREASE"
            )
            if managed.state == OrderState.CREATED:
                managed.transition(OrderState.SUBMITTING, source="LOCAL")

        if not reduce_only:
            from bot.critical_state import critical_state
            from bot.runtime_readiness import assert_ready_for_new_entries
            from bot.execution_ownership import (
                acquire_execution_ownership,
                validate_execution_ownership,
                publish_valid_execution_ownership,
            )

            critical_state.assert_available_for_new_risk()
            engine = getattr(self, "_engine", None)
            if engine is None:
                raise RuntimeError(
                    "READY_FOR_NEW_ENTRIES=false: execution engine unavailable"
                )
            ownership = getattr(self, "_execution_ownership", None)
            if ownership is None:
                ownership = await acquire_execution_ownership()
                self._execution_ownership = ownership
            await validate_execution_ownership(ownership)
            publish_valid_execution_ownership(
                engine, ownership, event="predispatch_validated"
            )
            assert_ready_for_new_entries(engine)

        data = await self._post(
            "/fapi/v1/order",
            params,
            single_attempt=single_submission,
        )
        order_id = str(data.get("orderId", "") or "")
        client_oid = str(data.get("clientOrderId") or oid)
        if order_id:
            self._order_id_symbol[order_id] = symbol
            self._client_oid_symbol[client_oid] = symbol
            if registry is not None and managed is not None:
                registry.index_order_id(order_id, oid)
                try:
                    managed.transition(
                        OrderState.SUBMITTED,
                        order_id=order_id,
                        source="REST",
                    )
                except InvalidTransition as exc:
                    log.warning(
                        "[ORDER_REGISTRY_ACK] clientOid=%s orderId=%s conflict=%s",
                        oid,
                        order_id,
                        exc,
                    )
            log.info(
                "📤 [BINANCE_ORDER] clientOid=%s orderId=%s symbol=%s "
                "side=%s qty=%s",
                client_oid,
                order_id,
                symbol,
                side_upper,
                qty_text,
            )
            if not reduce_only and (sl > 0 or tp > 0):
                await asyncio.sleep(0.15)
                if not await self.set_position_stops(
                    symbol, sl=sl, tp=tp
                ):
                    log.critical(
                        "[BINANCE_PROTECTION] %s entry accepted but "
                        "SL/TP not confirmed",
                        symbol,
                    )
        return {
            "orderId": order_id,
            "clientOid": client_oid,
            **data,
        }

    async def get_order_by_client_oid(self, client_oid: str) -> dict:
        symbol = self._client_oid_symbol.get(str(client_oid), "")
        if not symbol:
            return {}
        try:
            data = await self._get(
                "/fapi/v1/order",
                {
                    "symbol": to_binance(symbol),
                    "origClientOrderId": client_oid,
                },
                auth=True,
            )
        except Exception:
            return {}
        return self._normalize_order(data, symbol)

    async def _recover_ambiguous_order(
        self, endpoint: str, body: dict
    ) -> dict:
        oid = str(
            body.get("newClientOrderId") or body.get("clientOid") or ""
        )
        symbol = str(body.get("symbol") or "")
        if oid and symbol:
            self._client_oid_symbol[oid] = to_standard(symbol)
        for attempt in range(3):
            found = await self.get_order_by_client_oid(oid)
            if found:
                return found
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
        return {}

    async def _active_position_for_symbol(self, symbol: str):
        rows = await self.get_positions()
        return next(
            (
                position
                for position in rows
                if position.get("symbol") == symbol
                and abs(float(position.get("size", 0) or 0)) > 0
            ),
            None,
        )

    async def set_position_stops(
        self, symbol: str, sl: float = 0, tp: float = 0
    ) -> bool:
        if PAPER_TRADE:
            log.info(
                "[PAPER][BINANCE] set_position_stops %s SL=%s TP=%s",
                symbol,
                sl,
                tp,
            )
            return True
        if not _live_migration_ready():
            raise RuntimeError("BINANCE_LIVE_MIGRATION_NOT_RELEASED")

        position = await self._active_position_for_symbol(symbol)
        if not position:
            return False
        close_side = "SELL" if position.get("side") == "Buy" else "BUY"
        common = {
            "algoType": "CONDITIONAL",
            "symbol": to_binance(symbol),
            "side": close_side,
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "closePosition": "true",
        }
        submitted = 0
        expected = int(sl > 0) + int(tp > 0)
        if sl > 0:
            params = dict(common)
            params.update(
                {
                    "type": "STOP_MARKET",
                    "triggerPrice": self._round_price(sl, symbol),
                    "clientAlgoId": self.build_client_oid(
                        symbol,
                        close_side,
                        0,
                        f"sl:{symbol}:{sl}:{time.time_ns()}",
                    ),
                }
            )
            result = await self._post(
                "/fapi/v1/algoOrder",
                params,
                single_attempt=True,
            )
            if result.get("algoId"):
                submitted += 1

        if tp > 0:
            params = dict(common)
            params.update(
                {
                    "type": "TAKE_PROFIT_MARKET",
                    "triggerPrice": self._round_price(tp, symbol),
                    "clientAlgoId": self.build_client_oid(
                        symbol,
                        close_side,
                        0,
                        f"tp:{symbol}:{tp}:{time.time_ns()}",
                    ),
                }
            )
            result = await self._post(
                "/fapi/v1/algoOrder",
                params,
                single_attempt=True,
            )
            if result.get("algoId"):
                submitted += 1

        return expected > 0 and submitted == expected

    async def set_sl(self, symbol: str, sl: float, instruments=None):
        return await self.set_position_stops(symbol, sl=sl, tp=0)

    async def get_stop_orders(self, symbol: str):
        data = await self._get(
            "/fapi/v1/openAlgoOrders",
            {"symbol": to_binance(symbol)},
            auth=True,
        )
        rows = data if isinstance(data, list) else data.get("orders", [])
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            status = str(
                row.get("algoStatus", row.get("status", "NEW"))
            ).upper()
            out.append(
                {
                    "symbol": to_standard(row.get("symbol")),
                    "side": str(row.get("side", "")).lower(),
                    "status": status,
                    "stopPrice": row.get(
                        "triggerPrice", row.get("stopPrice", 0)
                    ),
                    "closeOrder": bool(
                        row.get("closePosition", False)
                    ),
                    "reduceOnly": bool(row.get("reduceOnly", False)),
                    "size": row.get(
                        "quantity", row.get("origQty", 0)
                    ),
                    "sizeUnit": "BASE_ASSET",
                    "orderId": str(row.get("algoId", "") or ""),
                    "clientOid": row.get("clientAlgoId", ""),
                    "isActive": status
                    in {"NEW", "WORKING", "PENDING"},
                }
            )
        return out

    async def get_open_orders(self, symbol: str = ""):
        params = {"symbol": to_binance(symbol)} if symbol else {}
        data = await self._get(
            "/fapi/v1/openOrders", params, auth=True
        )
        return data if isinstance(data, list) else []

    async def cancel_all_orders(self, symbol: str = "") -> bool:
        if PAPER_TRADE:
            log.info("[PAPER][BINANCE] cancel_all_orders skipped")
            return True
        if not symbol:
            raise ValueError("Binance cancel_all_orders requires symbol")
        normal_ok = False
        algo_ok = False
        try:
            await self._delete(
                "/fapi/v1/allOpenOrders",
                {"symbol": to_binance(symbol)},
            )
            normal_ok = True
        except Exception as exc:
            log.warning(
                "Binance cancel normal orders %s: %s", symbol, exc
            )
        try:
            await self._delete(
                "/fapi/v1/algoOpenOrders",
                {"symbol": to_binance(symbol)},
            )
            algo_ok = True
        except Exception as exc:
            log.warning(
                "Binance cancel algo orders %s: %s", symbol, exc
            )
        return normal_ok and algo_ok

    async def get_klines(
        self, symbol: str, interval: str, limit: int = 200
    ) -> list:
        binance_interval = INTERVAL_MAP.get(str(interval))
        if binance_interval is None:
            raise ValueError(
                f"unsupported Binance interval: {interval}"
            )
        data = await self._get(
            "/fapi/v1/klines",
            {
                "symbol": to_binance(symbol),
                "interval": binance_interval,
                "limit": min(1500, max(1, int(limit))),
            },
        )
        out = []
        for row in data if isinstance(data, list) else []:
            try:
                out.append(
                    {
                        "ts": int(row[0]),
                        "o": float(row[1]),
                        "h": float(row[2]),
                        "l": float(row[3]),
                        "c": float(row[4]),
                        "v": float(row[5]),
                    }
                )
            except (IndexError, TypeError, ValueError):
                continue
        out.sort(key=lambda item: item["ts"])
        dedup = {item["ts"]: item for item in out}
        out = [dedup[key] for key in sorted(dedup)]
        key = (symbol, str(interval))
        cache = self._kline_cache.setdefault(
            key, deque(maxlen=500)
        )
        cache.clear()
        cache.extend(out[-limit:])
        return out[-limit:]

    def get_cached_klines(
        self, symbol: str, interval: str, limit: int = 200
    ) -> list:
        cached = list(
            self._kline_cache.get((symbol, str(interval)), [])
        )
        if not cached:
            return []
        try:
            interval_minutes = INTERVAL_MINUTES[str(interval)]
            age = time.time() - cached[-1]["ts"] / 1000
            if (
                age
                > interval_minutes * 60 * self.STALE_MULTIPLIER
            ):
                return []
        except Exception:
            return []
        return cached[-limit:]

    async def get_ticker(self, symbol: str) -> dict:
        data = await self._get(
            "/fapi/v1/ticker/24hr",
            {"symbol": to_binance(symbol)},
        )
        ticker = {
            "symbol": symbol,
            "lastPrice": float(data.get("lastPrice", 0) or 0),
            "bid": float(data.get("bidPrice", 0) or 0),
            "ask": float(data.get("askPrice", 0) or 0),
            "volume": float(data.get("volume", 0) or 0),
            "turnover": float(data.get("quoteVolume", 0) or 0),
        }
        self._ticker_cache[symbol] = ticker
        return ticker

    def get_cached_ticker(self, symbol: str) -> dict:
        return self._ticker_cache.get(symbol, {})

    async def get_all_tickers(self) -> list:
        data = await self._get("/fapi/v1/ticker/24hr")
        out = []
        for row in data if isinstance(data, list) else []:
            symbol = str(row.get("symbol", "")).upper()
            if symbol not in self._instruments:
                continue
            try:
                out.append(
                    {
                        "symbol": symbol,
                        "turnover24h": float(
                            row.get("quoteVolume", 0) or 0
                        ),
                        "volume24h": float(
                            row.get("volume", 0) or 0
                        ),
                        "lastPrice": float(
                            row.get("lastPrice", 0) or 0
                        ),
                        "priceChgPct": float(
                            row.get("priceChangePercent", 0) or 0
                        )
                        / 100,
                    }
                )
            except (TypeError, ValueError):
                continue
        return out

    async def get_open_interest(self, symbol: str) -> dict:
        data = await self._get(
            "/fapi/v1/openInterest",
            {"symbol": to_binance(symbol)},
        )
        open_interest = float(
            data.get("openInterest", 0) or 0
        )
        try:
            ticker = (
                self.get_cached_ticker(symbol)
                or await self.get_ticker(symbol)
            )
            price = float(ticker.get("lastPrice", 0) or 0)
        except Exception:
            price = 0.0
        return {
            "openInterest": str(open_interest),
            "openInterestValue": str(
                open_interest * price if price > 0 else 0
            ),
        }

    async def get_funding_rate(self, symbol: str) -> float:
        data = await self._get(
            "/fapi/v1/premiumIndex",
            {"symbol": to_binance(symbol)},
        )
        return float(data.get("lastFundingRate", 0) or 0)

    def _normalize_order(
        self, data, symbol: str = ""
    ) -> dict:
        if not isinstance(data, dict) or not data:
            return {}
        status = str(data.get("status", "")).upper()
        executed = float(data.get("executedQty", 0) or 0)
        result = dict(data)
        result.update(
            {
                "orderId": str(data.get("orderId", "") or ""),
                "clientOid": data.get("clientOrderId", ""),
                "symbol": to_standard(
                    data.get("symbol") or symbol
                ),
                "isActive": status
                in {"NEW", "PARTIALLY_FILLED"},
                "filledSize": str(executed),
                "dealSize": str(executed),
                "dealValue": str(
                    data.get(
                        "cumQuote", data.get("cumQuoteQty", 0)
                    )
                    or 0
                ),
                "cancelExist": status
                in {
                    "CANCELED",
                    "EXPIRED",
                    "EXPIRED_IN_MATCH",
                    "REJECTED",
                },
            }
        )
        return result

    async def get_order_status(self, order_id: str) -> dict:
        if not order_id:
            return {
                "status": "UNKNOWN",
                "isActive": True,
                "filledSize": "0",
                "_unknown": True,
            }
        if (
            PAPER_TRADE
            or str(order_id).startswith("paper_")
            or order_id == "EXISTING_POSITION"
        ):
            return {
                "status": "FILLED",
                "isActive": False,
                "filledSize": "0",
                "_synthetic": True,
            }
        symbol = self._order_id_symbol.get(str(order_id), "")
        if not symbol:
            return {
                "status": "UNKNOWN",
                "isActive": True,
                "filledSize": "0",
                "_unknown": True,
            }
        try:
            data = await self._get(
                "/fapi/v1/order",
                {
                    "symbol": to_binance(symbol),
                    "orderId": order_id,
                },
                auth=True,
            )
        except Exception:
            return {}
        return self._normalize_order(data, symbol)

    async def wait_for_fill(
        self,
        order_id: str,
        timeout_s: float = 8.0,
        poll_interval_s: float = 0.5,
    ) -> dict:
        deadline = time.monotonic() + max(
            0.0, float(timeout_s)
        )
        last = {}
        while time.monotonic() < deadline:
            last = await self.get_order_status(order_id)
            if last.get("_synthetic"):
                return {
                    "filled": True,
                    "status": last,
                    "timed_out": False,
                }
            status = str(
                last.get("status", "")
            ).upper()
            if status == "FILLED":
                return {
                    "filled": True,
                    "status": last,
                    "timed_out": False,
                }
            if status in {
                "CANCELED",
                "EXPIRED",
                "EXPIRED_IN_MATCH",
                "REJECTED",
            }:
                return {
                    "filled": False,
                    "status": last,
                    "timed_out": False,
                }
            await asyncio.sleep(
                max(0.05, float(poll_interval_s))
            )
        return {
            "filled": False,
            "status": last,
            "timed_out": True,
        }

    async def get_orderbook(
        self, symbol: str, depth: int = 20
    ) -> dict:
        limit = 20 if depth <= 20 else 50
        data = await self._get(
            "/fapi/v1/depth",
            {"symbol": to_binance(symbol), "limit": limit},
        )
        orderbook = {
            "b": [
                [str(item[0]), str(item[1])]
                for item in data.get("bids", [])[:depth]
            ],
            "a": [
                [str(item[0]), str(item[1])]
                for item in data.get("asks", [])[:depth]
            ],
        }
        self._ob_cache[symbol] = orderbook
        return orderbook

    async def get_positions(self) -> list:
        data = await self._get(
            "/fapi/v3/positionRisk", auth=True
        )
        if not isinstance(data, list):
            raise RuntimeError(
                "POSITIONS_UNCONFIRMED: Binance "
                "positionRisk unavailable"
            )
        out = []
        active_by_symbol = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                quantity = float(
                    row.get("positionAmt", 0) or 0
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "POSITIONS_UNCONFIRMED: "
                    "invalid position amount"
                ) from exc
            if quantity == 0:
                continue
            symbol = to_standard(row.get("symbol"))
            active_by_symbol[symbol] = (
                active_by_symbol.get(symbol, 0) + 1
            )
            if active_by_symbol[symbol] > 1:
                raise RuntimeError(
                    "BINANCE_HEDGE_MODE_UNSUPPORTED: "
                    f"multiple active rows for {symbol}"
                )
            entry = float(
                row.get("entryPrice", 0) or 0
            )
            liquidation = float(
                row.get("liquidationPrice", 0) or 0
            )
            out.append(
                {
                    "symbol": symbol,
                    "side": "Buy"
                    if quantity > 0
                    else "Sell",
                    "size": abs(quantity),
                    "sizeUnit": "BASE_ASSET",
                    "entryPrice": entry,
                    "avgPrice": entry,
                    "markPrice": float(
                        row.get("markPrice", 0) or 0
                    ),
                    "unrealisedPnl": float(
                        row.get("unRealizedProfit", 0) or 0
                    ),
                    "leverage": float(
                        row.get("leverage", 1) or 1
                    ),
                    "liquidationPrice": liquidation,
                    "liqPrice": liquidation,
                    "stopLoss": 0.0,
                    "takeProfit": 0.0,
                    "posMargin": float(
                        row.get("isolatedMargin", 0) or 0
                    ),
                    "positionSide": row.get(
                        "positionSide", "BOTH"
                    ),
                    "marginType": row.get(
                        "marginType", ""
                    ),
                }
            )
        return out

    async def _seed_kline_cache(
        self, symbols: list, intervals: list
    ):
        semaphore = asyncio.Semaphore(4)

        async def one(symbol, interval):
            async with semaphore:
                try:
                    limits = {
                        "15": 200,
                        "60": 150,
                        "240": 120,
                    }
                    await self.get_klines(
                        symbol,
                        interval,
                        limits.get(str(interval), 120),
                    )
                except Exception as exc:
                    log.debug(
                        "Binance seed %s %s: %s",
                        symbol,
                        interval,
                        exc,
                    )

        await asyncio.gather(
            *(
                one(symbol, str(interval))
                for symbol in symbols
                for interval in intervals
            ),
            return_exceptions=True,
        )

    async def start_websocket(
        self, symbols: list, intervals: list = None
    ):
        intervals = [
            str(value)
            for value in (
                intervals or ["15", "60", "240"]
            )
        ]
        await self._seed_kline_cache(
            symbols, intervals
        )
        task = asyncio.create_task(
            self._ws_loop(list(symbols), intervals)
        )
        self._ws_tasks.add(task)
        task.add_done_callback(
            self._ws_tasks.discard
        )

    async def _ws_loop(
        self, symbols: list, intervals: list
    ):
        streams = []
        for symbol in symbols:
            lower = to_binance(symbol).lower()
            streams.append(f"{lower}@ticker")
            for interval in intervals:
                streams.append(
                    f"{lower}@kline_"
                    f"{INTERVAL_MAP[interval]}"
                )
        url = (
            f"{WS_BASE}/stream?streams="
            + "/".join(streams)
        )
        retry = 1.0
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=180,
                    ping_timeout=600,
                    close_timeout=5,
                    max_queue=2048,
                ) as websocket:
                    retry = 1.0
                    async for raw in websocket:
                        try:
                            message = json.loads(raw)
                            await self._handle_ws_message(
                                message
                            )
                        except Exception as exc:
                            log.debug(
                                "Binance public WS parse: %s",
                                exc,
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "Binance public WS reconnect: %s",
                    type(exc).__name__,
                )
                await asyncio.sleep(retry)
                retry = min(30.0, retry * 2)

    async def _handle_ws_message(self, message: dict):
        data = (
            message.get("data", message)
            if isinstance(message, dict)
            else {}
        )
        if not isinstance(data, dict):
            return
        event = data.get("e")
        if event == "kline":
            kline = data.get("k", {})
            symbol = to_standard(data.get("s"))
            interval = WS_INTERVAL_REV.get(
                str(kline.get("i", ""))
            )
            if not symbol or not interval:
                return
            row = {
                "ts": int(kline.get("t", 0)),
                "o": float(kline.get("o", 0)),
                "h": float(kline.get("h", 0)),
                "l": float(kline.get("l", 0)),
                "c": float(kline.get("c", 0)),
                "v": float(kline.get("v", 0)),
            }
            key = (symbol, interval)
            cache = self._kline_cache.setdefault(
                key, deque(maxlen=500)
            )
            if cache and cache[-1]["ts"] == row["ts"]:
                cache[-1] = row
            else:
                cache.append(row)
            return

        if event == "24hrTicker":
            symbol = to_standard(data.get("s"))
            if not symbol:
                return
            self._ticker_cache[symbol] = {
                "symbol": symbol,
                "lastPrice": float(
                    data.get("c", 0) or 0
                ),
                "bid": float(data.get("b", 0) or 0),
                "ask": float(data.get("a", 0) or 0),
                "volume": float(data.get("v", 0) or 0),
                "turnover": float(
                    data.get("q", 0) or 0
                ),
            }

    async def _listen_key_request(self, method: str):
        if not API_KEY:
            raise RuntimeError(
                "BINANCE_API_KEY unavailable"
            )
        await self._ensure_session()
        async with self._session.request(
            method,
            REST_BASE + "/fapi/v1/listenKey",
            headers={"X-MBX-APIKEY": API_KEY},
        ) as response:
            data = await response.json(
                content_type=None
            )
            if not (200 <= response.status < 300):
                raise RuntimeError(
                    "Binance listenKey "
                    f"HTTP {response.status}"
                )
            return data

    def start_private_websocket(
        self, order_registry, symbols: list
    ):
        self._order_registry = order_registry
        if PAPER_TRADE:
            log.info(
                "[PAPER][BINANCE] private "
                "user-data WS not required"
            )
            return None
        if (
            self._private_ws_task
            and not self._private_ws_task.done()
        ):
            return self._private_ws_task
        self._private_ws_task = asyncio.create_task(
            self._private_ws_loop()
        )
        return self._private_ws_task

    async def _private_ws_loop(self):
        retry = 1.0
        while True:
            keepalive = None
            try:
                data = await self._listen_key_request(
                    "POST"
                )
                listen_key = str(
                    data.get("listenKey", "")
                )
                if not listen_key:
                    raise RuntimeError(
                        "empty Binance listenKey"
                    )
                self._listen_key = listen_key

                async def ping_loop():
                    while True:
                        await asyncio.sleep(30 * 60)
                        await self._listen_key_request(
                            "PUT"
                        )

                keepalive = asyncio.create_task(
                    ping_loop()
                )

                async with websockets.connect(
                    f"{WS_BASE}/ws/{listen_key}",
                    ping_interval=180,
                    ping_timeout=600,
                    close_timeout=5,
                    max_queue=2048,
                ) as websocket:
                    retry = 1.0
                    async for raw in websocket:
                        message = json.loads(raw)
                        await self._handle_private_order_event(
                            message
                        )
            except asyncio.CancelledError:
                if keepalive:
                    keepalive.cancel()
                raise
            except Exception as exc:
                log.warning(
                    "Binance private WS reconnect: %s",
                    type(exc).__name__,
                )
                await asyncio.sleep(retry)
                retry = min(30.0, retry * 2)
            finally:
                if keepalive:
                    keepalive.cancel()

    async def _handle_private_order_event(
        self, message: dict
    ):
        if (
            not isinstance(message, dict)
            or message.get("e")
            != "ORDER_TRADE_UPDATE"
        ):
            return
        order = message.get("o", {})
        client_oid = str(
            order.get("c", "") or ""
        )
        order_id = str(
            order.get("i", "") or ""
        )
        symbol = to_standard(order.get("s"))
        status = str(
            order.get("X", "")
        ).upper()

        if order_id and symbol:
            self._order_id_symbol[order_id] = symbol
        if client_oid and symbol:
            self._client_oid_symbol[
                client_oid
            ] = symbol

        registry = self._order_registry
        if (
            registry is None
            or not client_oid.startswith("bgx7-")
        ):
            return
        try:
            managed, _ = registry.get_or_create(
                client_oid,
                symbol,
                order.get("S", ""),
                float(order.get("q", 0) or 0),
            )
            if order_id:
                registry.index_order_id(
                    order_id, client_oid
                )
        except Exception:
            return

        target = {
            "NEW": OrderState.SUBMITTED,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELLED,
            "EXPIRED": OrderState.CANCELLED,
            "EXPIRED_IN_MATCH": OrderState.CANCELLED,
            "REJECTED": OrderState.FAILED,
        }.get(status)

        if (
            target is not None
            and managed.state != target
        ):
            try:
                managed.transition(
                    target,
                    order_id=order_id or None,
                    source="WS",
                )
            except (InvalidTransition, TypeError):
                pass

    def get_cache_stats(self) -> dict:
        return {
            "klines": sum(
                len(value)
                for value in self._kline_cache.values()
            ),
            "kline_series": len(
                self._kline_cache
            ),
            "tickers": len(self._ticker_cache),
            "orderbooks": len(self._ob_cache),
            "exchange": "binance",
        }

    async def sync_time(self) -> bool:
        try:
            data = await self._get(
                "/fapi/v1/time"
            )
            server = int(data.get("serverTime"))
            local = int(time.time() * 1000)
            self._time_offset_ms = server - local
            self._time_synced = True
            return True
        except Exception as exc:
            log.warning(
                "Binance sync_time: %s", exc
            )
            self._time_synced = False
            return False

    def _now_ms(self) -> int:
        return (
            int(time.time() * 1000)
            + int(self._time_offset_ms)
        )

    async def ping(self) -> bool:
        try:
            await self._get("/fapi/v1/ping")
            return True
        except Exception:
            return False

    async def close(self):
        tasks = list(self._ws_tasks)
        if self._private_ws_task:
            tasks.append(
                self._private_ws_task
            )
        for task in tasks:
            if task and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(
                *tasks, return_exceptions=True
            )
        if (
            self._session
            and not self._session.closed
        ):
            await self._session.close()
