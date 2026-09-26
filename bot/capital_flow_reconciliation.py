"""Read-only exchange ledger reconciliation for external capital flows.

KuCoin can provide transfer rows with a post-flow accountEquity anchor, so its
existing path can safely rebase the durable high-water mark when the evidence
matches. Binance USD-M income rows expose TRANSFER identity/amount but not a
post-flow equity anchor. Binance therefore bootstraps by checkpointing already
observed transfer identities without rebasing; any newly observed TRANSFER then
fails closed until an operator can reconcile it with stronger evidence.

This module never mutates the exchange and never authorizes an order.
"""
from __future__ import annotations

import json
import math
import time

from bot import database as db
from bot.drawdown_persistence import rebase_real_account_peak_for_external_flow
from bot.logger import log

# v2 intentionally supersedes the first bootstrap cursor. The v1 bootstrap
# assumed positive transfer amounts and could checkpoint past a signed KuCoin
# TransferOut without applying it. v2 rescans the bounded recent ledger once.
LAST_FLOW_OFFSET_KEY = "risk:external_capital_flow:last_offset:v2"
BINANCE_LAST_FLOW_CURSOR_KEY = "risk:external_capital_flow:binance:seen_transfers:v1"


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


def _transfer_amount(value) -> float:
    """Normalize KuCoin's signed or unsigned transfer ledger amount."""
    out = _finite(value, "ledger transfer amount")
    if out == 0:
        raise ValueError("ledger transfer amount must be nonzero")
    return abs(out)


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

    amount = _transfer_amount(row.get("amount"))
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


def _is_binance_client(client) -> bool:
    """Capability detection without importing the active adapter at module load."""
    return callable(getattr(client, "_listen_key_request", None))


def _binance_transfer_identity(row: dict) -> str:
    if not isinstance(row, dict):
        raise ValueError("invalid Binance income row")
    if str(row.get("incomeType") or "").upper() != "TRANSFER":
        raise ValueError("not a Binance transfer row")
    if str(row.get("asset") or "USDT").upper() != "USDT":
        raise ValueError("unsupported Binance transfer asset")
    event_time = int(_finite(row.get("time", 0), "Binance transfer time"))
    if event_time <= 0:
        raise ValueError("Binance transfer time must be positive")
    tran_id = str(row.get("tranId") or "").strip()
    if not tran_id:
        raise ValueError("Binance transfer tranId missing")
    amount = _finite(row.get("income"), "Binance transfer income")
    if amount == 0:
        raise ValueError("Binance transfer income must be nonzero")
    return f"{event_time}:{tran_id}"


async def _fetch_binance_transfers(client) -> list[dict]:
    # Keep the evidence window aligned with the accounting ledger auditor.
    # A first-observation checkpoint is identity-only; it never infers equity.
    from bot.binance_accounting_evidence import collect_income

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 48 * 3600000
    rows = await collect_income(client, start_ms, end_ms)
    transfers: list[dict] = []
    for row in rows:
        if str(row.get("incomeType") or "").upper() != "TRANSFER":
            continue
        if str(row.get("asset") or "USDT").upper() != "USDT":
            continue
        identity = _binance_transfer_identity(row)
        transfers.append({
            "identity": identity,
            "time": int(row.get("time", 0) or 0),
            "tranId": str(row.get("tranId") or ""),
            "amount": float(row.get("income", 0) or 0),
        })
    transfers.sort(key=lambda item: (item["time"], item["tranId"]))
    return transfers


async def _reconcile_binance_capital_flows(
    client,
    current_equity: float,
    *,
    strict: bool,
) -> dict:
    """Checkpoint historical Binance transfers; block on any later new one.

    Binance /fapi/v1/income does not provide a post-transfer account-equity
    anchor. Reconstructing pre/post equity from the amount would silently mix
    market PnL with cash flows, so automatic HWM rebasing is intentionally
    prohibited until stronger evidence is available.
    """
    transfers = await _fetch_binance_transfers(client)
    identities = [item["identity"] for item in transfers]
    raw_cursor = await db.load_key_value(
        BINANCE_LAST_FLOW_CURSOR_KEY,
        strict=strict,
    )

    if raw_cursor is None:
        checkpoint = json.dumps(
            {
                "version": 1,
                "seen": identities,
                "observed_at_ms": int(time.time() * 1000),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        ok = await db.save_key_value(
            BINANCE_LAST_FLOW_CURSOR_KEY,
            checkpoint,
            strict=strict,
        )
        if strict and not ok:
            raise db.PersistenceError(
                "Binance capital-flow checkpoint write not confirmed"
            )
        log.warning(
            "[CAPITAL_FLOW_BINANCE] bootstrap=checkpoint_only transfers=%d "
            "rebase=false reason=post_equity_anchor_unavailable "
            "execution_effect=NONE",
            len(transfers),
        )
        return {
            "applied": 0,
            "bootstrap": True,
            "observed": len(transfers),
            "authority": "checkpoint_only",
        }

    try:
        cursor = json.loads(raw_cursor)
        if not isinstance(cursor, dict) or int(cursor.get("version", 0)) != 1:
            raise ValueError("invalid version")
        seen = cursor.get("seen")
        if not isinstance(seen, list) or any(
            not isinstance(item, str) or not item for item in seen
        ):
            raise ValueError("invalid seen identities")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise db.PersistenceError(
            "Binance capital-flow cursor is malformed"
        ) from exc

    seen_set = set(seen)
    new_transfers = [
        item for item in transfers if item["identity"] not in seen_set
    ]
    if new_transfers:
        net_amount = sum(float(item["amount"]) for item in new_transfers)
        log.critical(
            "[CAPITAL_FLOW_BINANCE] new_transfers=%d net_amount=%.8f "
            "rebase=false reason=post_equity_anchor_unavailable "
            "action=BLOCK_NEW_ENTRY",
            len(new_transfers),
            net_amount,
        )
        if strict:
            raise RuntimeError(
                "BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED"
            )
        return {
            "applied": 0,
            "bootstrap": False,
            "blocked": True,
            "new_transfers": len(new_transfers),
        }

    log.info(
        "[CAPITAL_FLOW_BINANCE] transfers=%d new=0 rebase=false "
        "execution_effect=NONE",
        len(transfers),
    )
    return {
        "applied": 0,
        "bootstrap": False,
        "blocked": False,
        "observed": len(transfers),
    }


def _equity_matches(post_equity: float, current_equity: float) -> bool:
    # Bootstrap is intentionally conservative: only reconcile a ledger event
    # whose post-transfer equity still matches the currently observed account.
    tolerance = max(0.02, abs(current_equity) * 0.001)
    return abs(post_equity - current_equity) <= tolerance


async def reconcile_external_capital_flows(client, risk, current_equity: float, *, strict: bool = True) -> dict:
    """Reconcile external cash flows using exchange-native evidence semantics."""
    current_equity = _positive(current_equity, "current equity")

    if _is_binance_client(client):
        return await _reconcile_binance_capital_flows(
            client,
            current_equity,
            strict=strict,
        )

    # KuCoin path: post-transfer accountEquity permits an evidence-anchored HWM
    # rebase. Historical transfers are never replayed blindly.
    transfers = await _fetch_transfers(client)
    if not transfers:
        log.info("[CAPITAL_FLOW_LEDGER] transfers=0 action=none execution_effect=NONE")
        return {"applied": 0, "bootstrap": False}

    raw_offset = await db.load_key_value(LAST_FLOW_OFFSET_KEY, strict=strict)
    newest_offset = max(item["offset"] for item in transfers)

    if raw_offset is None:
        matches = [item for item in transfers if _equity_matches(item["post_equity"], current_equity)]
        matched = max(matches, key=lambda x: (x["offset"], x["time"])) if matches else None
        applied = 0
        if matched is not None:
            await rebase_real_account_peak_for_external_flow(
                risk,
                current_equity,
                pre_flow_equity=matched["pre_equity"],
                post_flow_equity=matched["post_equity"],
                flow_type=matched["type"],
                flow_amount=matched["amount"],
                flow_offset=str(matched["offset"]),
                strict=strict,
            )
            applied = 1
            log.warning(
                "[CAPITAL_FLOW_LEDGER] bootstrap=matched type=%s amount=%.4f offset=%s",
                matched["type"],
                matched["amount"],
                matched["offset"],
            )
        else:
            newest = max(transfers, key=lambda x: (x["offset"], x["time"]))
            log.warning(
                "[CAPITAL_FLOW_LEDGER] bootstrap=cursor_only newest_post=%.4f current=%.4f "
                "reason=no_matching_post_equity execution_effect=NONE",
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
