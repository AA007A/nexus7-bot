"""Read-only Futures account balance observability.

Logs the full account-overview balance decomposition without changing sizing,
risk, trading mode, exchange mutations, or release gates.
"""
import math
import time


def _num(data, key):
    value = data.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _fmt(value):
    return "N/A" if value is None else f"{value:.4f}"


def install(log):
    from bot.kucoin import KuCoinClient

    if getattr(KuCoinClient, "_account_balance_observability_patched", False):
        return

    original_get_balance = KuCoinClient.get_balance

    async def _get_account_overview_snapshot(self):
        data = await self._get(
            "/api/v1/account-overview", {"currency": "USDT"}, auth=True
        )
        if not isinstance(data, dict):
            self._last_account_overview_snapshot = None
            return {}

        keys = (
            "accountEquity",
            "marginBalance",
            "availableBalance",
            "unrealisedPNL",
            "positionMargin",
            "orderMargin",
            "frozenFunds",
        )
        snap = {key: _num(data, key) for key in keys}
        snap["currency"] = data.get("currency") or "USDT"
        snap["_observed_at"] = time.time()
        self._last_account_overview_snapshot = dict(snap)
        return snap

    async def _get_balance_with_observability(self):
        bal = await original_get_balance(self)
        try:
            snap = await _get_account_overview_snapshot(self)
            if snap:
                log.info(
                    "[ACCOUNT_OVERVIEW] currency=%s equity=%s marginBalance=%s "
                    "available=%s unrealisedPnL=%s positionMargin=%s "
                    "orderMargin=%s frozenFunds=%s source=KuCoin read_only=true",
                    snap.get("currency", "USDT"),
                    _fmt(snap.get("accountEquity")),
                    _fmt(snap.get("marginBalance")),
                    _fmt(snap.get("availableBalance")),
                    _fmt(snap.get("unrealisedPNL")),
                    _fmt(snap.get("positionMargin")),
                    _fmt(snap.get("orderMargin")),
                    _fmt(snap.get("frozenFunds")),
                )
                equity = snap.get("accountEquity")
                available = snap.get("availableBalance")
                if equity is not None and equity > 0 and available is not None:
                    ratio = available / equity
                    if ratio < 0.20:
                        log.warning(
                            "[ACCOUNT_BALANCE_DIVERGENCE] available/equity=%.2f%% "
                            "available=%s equity=%s; investigate positionMargin/orderMargin/" 
                            "frozenFunds before LIVE release",
                            ratio * 100.0,
                            _fmt(available),
                            _fmt(equity),
                        )
        except Exception as exc:
            self._last_account_overview_snapshot = None
            log.warning(
                "[ACCOUNT_OVERVIEW] diagnostic read failed: %s; primary balance path unchanged",
                type(exc).__name__,
            )
        return bal

    KuCoinClient.get_account_overview_snapshot = _get_account_overview_snapshot
    KuCoinClient.get_balance = _get_balance_with_observability
    KuCoinClient._account_balance_observability_patched = True
    log.info(
        "[ACCOUNT_OVERVIEW] read-only balance decomposition installed; sizing/risk unchanged"
    )
