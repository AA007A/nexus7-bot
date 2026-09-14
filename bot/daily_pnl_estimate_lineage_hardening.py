"""Attach authoritative local-estimate metadata before durable daily-PnL writes.

Some engine-owned close paths create a ``Trade`` and call
``durable_daily_pnl.checkpoint(engine, extra=trade)`` while the corresponding
Position is still present.  Those values are operational estimates until KuCoin
position-history/fill evidence arrives, so they must be tagged consistently and
must carry the exact opening-order lineage when post-trade forensics captured it.

This module changes accounting metadata only.  It does not initiate, cancel,
resize, or authorize orders and does not alter close triggers, NEXUS, leverage,
sizing, SL/TP, TPSL, drawdown, or exchange execution semantics.
"""
from __future__ import annotations


_ESTIMATED_SOURCE = "ESTIMATED_LOCAL_MARK_AND_FEE_RATE"


def enrich_estimate(engine, trade, log=None):
    """Enrich an engine-owned local close estimate from its still-live Position.

    Returns ``trade`` unchanged when there is no matching tracked position.  A
    missing lineage is not invented: the event is still correctly classified as
    an estimate and later reconciliation may use the legacy unique symbol/time
    fallback.  When exact opening lineage exists, it is copied before the first
    durable checkpoint so KuCoin reconciliation can prefer OPENING_ORDER_ID.
    """
    if trade is None:
        return trade
    symbol = str(getattr(trade, "symbol", "") or "")
    positions = getattr(engine, "positions", {}) or {}
    pos = positions.get(symbol) if isinstance(positions, dict) else None
    if pos is None:
        return trade

    trade.accounting_source = _ESTIMATED_SOURCE
    lineage = getattr(pos, "_forensic_lineage", None)
    opening_order_id = ""
    if isinstance(lineage, dict):
        opening_order_id = str(lineage.get("order_id") or "")
    if opening_order_id:
        trade.opening_order_id = opening_order_id

    if log is not None:
        log.info(
            "[DAILY_PNL_ESTIMATE_LINEAGE] symbol=%s source=%s opening_order_id=%s "
            "durable_write_pending=true execution_effect=NONE",
            symbol or "NA",
            _ESTIMATED_SOURCE,
            "PRESENT" if opening_order_id else "ABSENT",
        )
    return trade


def install(durable_daily_pnl, log) -> None:
    if getattr(durable_daily_pnl, "_estimate_lineage_hardening_installed", False):
        return

    original_checkpoint = durable_daily_pnl.checkpoint

    async def checkpoint_with_estimate_lineage(engine, extra=None, now=None):
        if extra is not None:
            enrich_estimate(engine, extra, log)
        return await original_checkpoint(engine, extra=extra, now=now)

    durable_daily_pnl.checkpoint = checkpoint_with_estimate_lineage
    durable_daily_pnl._estimate_lineage_hardening_installed = True
    log.warning(
        "[DAILY_PNL_ESTIMATE_LINEAGE] installed=true direct_close_estimates=true "
        "opening_order_id_before_first_checkpoint=true missing_lineage_invented=false "
        "close_logic_unchanged=true leverage_unchanged=true sizing_unchanged=true "
        "execution_effect=ACCOUNTING_METADATA_ONLY"
    )
