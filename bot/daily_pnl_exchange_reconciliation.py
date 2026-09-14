"""Bridge confirmed exchange accounting evidence into the durable daily PnL ledger.

This wrapper is accounting-only. It does not submit/cancel orders, change leverage,
change sizing, alter signals, or authorize entries. The conservative local PnL
estimate remains in force until the existing exchange-accounting audit proves
BGX ownership, fills and durable lineage.
"""
from __future__ import annotations

from contextvars import ContextVar


_engine_context = ContextVar("daily_pnl_exchange_engine", default=None)


def install(exchange_accounting_evidence, log) -> None:
    if getattr(exchange_accounting_evidence, "_daily_pnl_reconciliation_installed", False):
        return

    original_audit = exchange_accounting_evidence.audit
    original_classify = exchange_accounting_evidence._classify_origin

    async def _classify_with_daily_pnl(client, receipt, registry, row):
        origin, reason = await original_classify(client, receipt, registry, row)
        if origin == "BGX_CONFIRMED":
            engine = _engine_context.get()
            if engine is None:
                log.warning(
                    "[DURABLE_DAILY_PNL_RECONCILE] symbol=%s result=NO_ENGINE_CONTEXT "
                    "adjustment=NONE risk_policy_unchanged=true",
                    row.get("symbol", "NA") if isinstance(row, dict) else "NA",
                )
            else:
                # _classify_origin only returns BGX_CONFIRMED when the existing
                # accounting pipeline has durable order IDs, reconciled fills
                # and reconciled opening lineage. Reuse that exact evidence.
                verified = dict(receipt)
                verified["lineage_reconciled"] = True
                from bot.durable_daily_pnl import reconcile_confirmed_exchange
                await reconcile_confirmed_exchange(engine, row, verified)
        return origin, reason

    async def _audit_with_context(engine):
        token = _engine_context.set(engine)
        try:
            return await original_audit(engine)
        finally:
            _engine_context.reset(token)

    exchange_accounting_evidence._classify_origin = _classify_with_daily_pnl
    exchange_accounting_evidence.audit = _audit_with_context
    exchange_accounting_evidence._daily_pnl_reconciliation_installed = True
    log.info(
        "[DURABLE_DAILY_PNL_RECONCILE] installed=true source=KUCOIN_CONFIRMED_ACCOUNTING "
        "estimate_until_confirmed=true double_count_guard=true leverage_unchanged=true "
        "sizing_unchanged=true execution_effect=NONE"
    )
