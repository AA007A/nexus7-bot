"""KuCoin Futures native TP/SL hardening for protected LIVE entries.

KuCoin does not expose a `/api/v1/position/trading-stop` mutation. The
supported Futures endpoint for an entry carrying both take-profit and
stop-loss is `POST /api/v1/st-orders`.

This adapter changes only opening orders that already carry an SL and/or TP.
Reduce-only exits and unprotected/no-TP-SL calls remain on the existing client
implementation. Existing AI, risk, EV/RR, pilot, durable-submission and
exposure gates are untouched.
"""
from __future__ import annotations

from bot.config import cfg

TPSL_ENDPOINT = "/api/v1/st-orders"


def _trigger_prices(side: str, sl: float, tp: float) -> tuple[float, float]:
    """Return (trigger_up, trigger_down) for a one-way futures entry."""
    side_l = str(side or "").strip().lower()
    if side_l == "buy":
        # Long: TP above, SL below.
        return float(tp or 0), float(sl or 0)
    if side_l == "sell":
        # Short: SL above, TP below.
        return float(sl or 0), float(tp or 0)
    raise ValueError(f"unsupported KuCoin order side: {side}")


def install(KuCoinClient, kucoin_module, log) -> None:
    if getattr(KuCoinClient, "_native_tpsl_entry_installed", False):
        return

    original = KuCoinClient.place_order

    async def place_order_with_native_tpsl(
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
        # Preserve every existing path except a protected LIVE opening order.
        if (
            kucoin_module.PAPER_TRADE
            or reduce_only
            or not (float(sl or 0) > 0 or float(tp or 0) > 0)
        ):
            return await original(
                self,
                symbol,
                side,
                qty,
                sl=sl,
                tp=tp,
                instruments=instruments,
                reduce_only=reduce_only,
                idem_key=idem_key,
                single_submission=single_submission,
            )

        if not kucoin_module.API_KEY:
            log.warning("place_order TPSL: KUCOIN_API_KEY não configurado")
            return {}

        try:
            contracts = self._round_qty(qty, symbol)
            client_oid = self.build_client_oid(
                symbol, side, qty, idem_key, contracts=contracts
            )
            trigger_up, trigger_down = _trigger_prices(side, sl, tp)
        except Exception as exc:
            log.error("[KUCOIN_NATIVE_TPSL] build failed symbol=%s error=%s", symbol, exc)
            return {}

        body = {
            "clientOid": client_oid,
            "symbol": kucoin_module.to_kucoin(symbol),
            "side": str(side).lower(),
            "type": "market",
            "size": int(contracts),
            "leverage": int(cfg.LEVERAGE),
            "reduceOnly": False,
            "marginMode": "CROSS",
            "positionSide": "BOTH",
            # TP = trade price trigger, matching KuCoin's documented TPSL example.
            "stopPriceType": "TP",
        }
        if trigger_up > 0:
            body["triggerStopUpPrice"] = self._round_price(trigger_up, symbol)
        if trigger_down > 0:
            body["triggerStopDownPrice"] = self._round_price(trigger_down, symbol)

        # Require at least one valid protection trigger. Fail closed before network I/O.
        if "triggerStopUpPrice" not in body and "triggerStopDownPrice" not in body:
            log.error("[KUCOIN_NATIVE_TPSL] blocked symbol=%s reason=no_valid_trigger", symbol)
            return {}

        post_options = {
            "single_attempt": True
        } if single_submission else {}
        data = await self._post(TPSL_ENDPOINT, body, **post_options)
        if not isinstance(data, dict):
            return {}

        order_id = data.get("orderId", "")
        if not order_id and data.get("_ambiguous"):
            # The normal order lookup endpoint indexes TPSL entries by clientOid too.
            # Never blindly resubmit an ambiguous LIVE mutation.
            for attempt in range(3):
                recovered = await self.get_order_by_client_oid(client_oid)
                if recovered:
                    data = recovered
                    order_id = recovered.get("orderId", "")
                    log.warning(
                        "♻️ [TPSL ORDER RECOVERED] clientOid=%s orderId=%s",
                        client_oid, order_id,
                    )
                    break
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(0.25 * (attempt + 1))

        if not order_id:
            log.error(
                "[KUCOIN_NATIVE_TPSL] submission unresolved symbol=%s clientOid=%s",
                symbol, client_oid,
            )
            return data

        data["clientOid"] = client_oid
        data["native_tpsl"] = True
        data["protection_endpoint"] = TPSL_ENDPOINT
        log.info(
            "📤 [ORDER+TPSL] clientOid=%s orderId=%s symbol=%s side=%s qty=%s "
            "SL=%s TP=%s endpoint=%s",
            client_oid, order_id, symbol, side, contracts,
            body.get("triggerStopDownPrice") if str(side).lower() == "buy" else body.get("triggerStopUpPrice"),
            body.get("triggerStopUpPrice") if str(side).lower() == "buy" else body.get("triggerStopDownPrice"),
            TPSL_ENDPOINT,
        )
        return data

    KuCoinClient.place_order = place_order_with_native_tpsl
    KuCoinClient._native_tpsl_entry_installed = True
    log.warning(
        "[KUCOIN_NATIVE_TPSL] installed: protected opening orders use "
        "POST /api/v1/st-orders; legacy /api/v1/position/trading-stop is bypassed"
    )
