"""Read-only KuCoin ledger reconciliation for external capital flows.

This module distinguishes deposits/transfers from trading PnL for LIVE drawdown.
It never mutates the exchange and never authorizes an order. Only completed
TransferIn/TransferOut ledger records are eligible.
"""
from __future__ import annotations

import math

from bot import database as db
from bot.drawdown_persistence import rebase_real_account_peak_for_external_flow
from bot.logger import log

LAST_FLOW_OFFSET_KEY = "risk:external_capital_flow:last_offset:v1"


def _finite(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} boolean")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{label} nonfinite")
    return out


def _positive(value, label: str) -> float:
    out = _finite(value, label)
    if out <= 0:
        raise ValueError(f"{label} must be positive")
    return out


def _offset_int(value) -> int:
    if isinstance(value, bool):
        raise ValueError("ledger offset boolean")
    return int(str(value))


def _normalized_transfer(row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    flow_type = str(row.get("type") or "").strip().lower()
    if flow_type not in {"transferin", "transferout"}:
        return None
    if str(row.get("status") or "").strip().lower() != "completed":
        return None

    amount = _positive(row.get("amount"), "ledger transfer amount")
    post_equity = _positive(row.get("accountEquity"), "ledger accountEquity")
    offset = _offset_int(row.get("offset"))
    event_time = int(_finite(row.get("time", 0), "ledger time"))

    if flow_type == "transferout":
        pre_equity = post_equity + amount
    else:
        pre_equity = post_equity - amount
        if pre_equity <= 0:
            raise ValueError("TransferIn pre-flow equity must be positive")

    return {
        "type": "TransferOut" if flow_type == "transferout" else "TransferIn",
        "amount": amount,
        "pre_equity": pre_equity,
        "post_equity": post_equity,
        "offset": offset,
        "time": event_time,
    }


async def _fetch_transfers(client) -> list[dict]:
    payload = await client._get(
        "/api/v1/transaction-history",
        {"currency": "USDT", "maxCount": 50},
        auth=True,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("futures ledger unavailable")

    rows = payload.get("dataList")
    if not isinstance(rows, list):
        raise RuntimeError("futures ledger dataList unavailable")

    transfers: list[dict] = []
    for row in rows:
        try:
            item = _normalized_transfer(row)
        except (TypeError, ValueError) as exc:
            log.warning(
                "[CAPITAL_FLOW_LEDGER] malformed transfer ignored reason=%s execution_effect=NONE",
                type(exc).__name__,
            )
            continue
        if item is not None:
            transfers.append(item)

    transfers.sort(key=lambda x: (x["offset"], x["time"]))
    return transfers


def _equity_matches(post_equity: float, current_equity: float) -> bool:
    # Bootstrap is intentionally conservative: only reconcile a ledger event
    # whose post-transfer equity still matches the currently observed account.
    tolerance = max(0.02, abs(current_equity) * 0.001)
    return abs(post_equity - current_equity) <= tolerance


async def reconcile_external_capital_flows(client, risk, current_equity: float, *, strict: bool = True) -> dict:
    """Apply newly verified KuCoin external cash flows to the durable HWM.

    First-install bootstrap behavior is fail-safe. Historical transfers are not
    replayed blindly. With no stored ledger cursor, at most the newest completed
    transfer is applied, and only when its post-transfer accountEquity matches
    the account equity observed by the runtime. The newest offset is then
    checkpointed so older events can never be double counted.
    """
    current_equity = _positive(current_equity, "current equity")
    transfers = await _fetch_transfers(client)
    if not transfers:
        log.info("[CAPITAL_FLOW_LEDGER] transfers=0 action=none execution_effect=NONE")
        return {"applied": 0, "bootstrap": False}

    raw_offset = await db.load_key_value(LAST_FLOW_OFFSET_KEY, strict=strict)
    newest_offset = max(item["offset"] for item in transfers)

    if raw_offset is None:
        newest = max(transfers, key=lambda x: (x["offset"], x["time"]))
        applied = 0
        if _equity_matches(newest["post_equity"], current_equity):
            await rebase_real_account_peak_for_external_flow(
                risk,
                current_equity,
                pre_flow_equity=newest["pre_equity"],
                post_flow_equity=newest["post_equity"],
                flow_type=newest["type"],
                flow_amount=newest["amount"],
                flow_offset=str(newest["offset"]),
                strict=strict,
            )
            applied = 1
            log.warning(
                "[CAPITAL_FLOW_LEDGER] bootstrap=matched type=%s amount=%.4f offset=%s",
                newest["type"],
                newest["amount"],
                newest["offset"],
            )
        else:
            log.warning(
                "[CAPITAL_FLOW_LEDGER] bootstrap=cursor_only newest_post=%.4f current=%.4f "
                "reason=equity_mismatch execution_effect=NONE",
                newest["post_equity"],
                current_equity,
            )

        ok = await db.save_key_value(LAST_FLOW_OFFSET_KEY, str(newest_offset), strict=strict)
        if strict and not ok:
            raise db.PersistenceError("capital-flow ledger cursor write not confirmed")
        return {"applied": applied, "bootstrap": True, "last_offset": newest_offset}

    try:
        last_offset = _offset_int(raw_offset)
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError("capital-flow ledger cursor is malformed") from exc

    pending = [item for item in transfers if item["offset"] > last_offset]
    applied = 0
    for item in pending:
        await rebase_real_account_peak_for_external_flow(
            risk,
            current_equity,
            pre_flow_equity=item["pre_equity"],
            post_flow_equity=item["post_equity"],
            flow_type=item["type"],
            flow_amount=item["amount"],
            flow_offset=str(item["offset"]),
            strict=strict,
        )
        ok = await db.save_key_value(LAST_FLOW_OFFSET_KEY, str(item["offset"]), strict=strict)
        if strict and not ok:
            raise db.PersistenceError("capital-flow ledger cursor write not confirmed")
        applied += 1

    log.info(
        "[CAPITAL_FLOW_LEDGER] pending=%d applied=%d last_offset=%s execution_effect=NONE",
        len(pending),
        applied,
        newest_offset,
    )
    return {"applied": applied, "bootstrap": False, "last_offset": newest_offset}
