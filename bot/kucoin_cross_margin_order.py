"""KuCoin Futures order margin-mode compatibility hardening.

KuCoin's current Futures Add Order API accepts a ``marginMode`` field. The
production account is operated in cross-margin mode; omitting this field can be
interpreted as an incompatible selected mode and returns error 330005.

This runtime adapter only normalizes NEXUS-created order request payloads. It
does not switch the account/position margin mode, does not query or mutate any
manual/external position, and does not authorize execution. Existing release,
PilotGuard, exposure, AI, risk, persistence and protection gates remain
unchanged.
"""
from __future__ import annotations


ORDER_ENDPOINT = "/api/v1/orders"
CROSS_MARGIN_MODE = "CROSS"


def install(KuCoinClient, log) -> None:
    if getattr(KuCoinClient, "_cross_margin_order_mode_patched", False):
        return

    original_post = KuCoinClient._post

    async def _post_with_cross_margin(self, endpoint, body, *args, **kwargs):
        if endpoint == ORDER_ENDPOINT and isinstance(body, dict):
            body = dict(body)
            body["marginMode"] = CROSS_MARGIN_MODE
        return await original_post(self, endpoint, body, *args, **kwargs)

    KuCoinClient._post = _post_with_cross_margin
    KuCoinClient._cross_margin_order_mode_patched = True
    log.warning(
        "[KUCOIN_ORDER_MARGIN_MODE] installed: /api/v1/orders explicitly uses "
        "marginMode=CROSS; account mode is never switched by this adapter"
    )
