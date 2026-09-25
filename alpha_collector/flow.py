"""Trade normalization and exact 1-minute flow aggregation (AGGREGATION.trade_flow_1m)."""
from __future__ import annotations

from decimal import Decimal

from alpha_collector import contract as C

BUCKET_MS = 60_000


def ns_to_ms(ts) -> int:
    t = int(ts)
    return t // 1_000_000 if t > 10 ** 15 else (t // 1000 if t > 10 ** 13 else t)


def trade_record(msg: dict, *, observed_ts: int, instance_id: str, epoch_id: str, canonical_symbol: str) -> dict:
    d = msg.get("data") or {}
    side = d.get("side")
    flags = []
    if side not in ("buy", "sell"):
        flags.append("MISSING")
        side = None
    rec = C.make_record(symbol=canonical_symbol, metric="trade", event_ts=ns_to_ms(d["ts"]) if d.get("ts") else None,
                        observed_ts=observed_ts, value=d.get("size"), units="lots", source_channel="ws:execution",
                        raw=msg, instance_id=instance_id, epoch_id=epoch_id, sequence=d.get("sequence"), flags=flags,
                        payload={"price": str(d.get("price")) if d.get("price") is not None else None,
                                 "size": str(d.get("size")) if d.get("size") is not None else None,
                                 "taker_side_venue": side, "venue_symbol": d.get("symbol"),
                                 "trade_id": str(d.get("tradeId")) if d.get("tradeId") is not None else None,
                                 "ts_ns": str(d.get("ts"))})
    rec["venue_trade_id"] = str(d.get("tradeId"))
    return rec


def aggregate(trades: list, multipliers: dict) -> dict:
    """{(symbol, bucket_ts): fields} computed with Decimal from the venue strings (exactly reproducible)."""
    out = {}
    for t in sorted(trades, key=lambda r: (r["symbol"], r["event_ts"], r["venue_trade_id"])):
        p = t["payload"]
        if p.get("price") is None or p.get("size") is None or p.get("taker_side_venue") is None:
            continue
        price, size = Decimal(p["price"]), Decimal(p["size"])
        mult = Decimal(str(multipliers[t["symbol"]]))
        notional = price * size * mult
        k = (t["symbol"], t["event_ts"] // BUCKET_MS * BUCKET_MS)
        a = out.setdefault(k, {"buy_notional": Decimal(0), "sell_notional": Decimal(0), "buy_count": 0, "sell_count": 0,
                               "px_size": Decimal(0), "size": Decimal(0), "high": price, "low": price, "trades": []})
        side = p["taker_side_venue"]
        a[f"{side}_notional"] += notional
        a[f"{side}_count"] += 1
        a["px_size"] += price * size
        a["size"] += size
        a["high"], a["low"] = max(a["high"], price), min(a["low"], price)
        a["trades"].append(t["venue_trade_id"])
    res = {}
    for k, a in out.items():
        tot = a["buy_notional"] + a["sell_notional"]
        res[k] = {"buy_notional": str(a["buy_notional"]), "sell_notional": str(a["sell_notional"]),
                  "total_notional": str(tot), "buy_count": a["buy_count"], "sell_count": a["sell_count"],
                  "total_count": a["buy_count"] + a["sell_count"],
                  "imbalance": str((a["buy_notional"] - a["sell_notional"]) / tot) if tot > 0 else None,
                  "vwap": str(a["px_size"] / a["size"]) if a["size"] > 0 else None,
                  "high": str(a["high"]), "low": str(a["low"]), "n_trade_ids": len(a["trades"])}
    return res


def flow_records(agg: dict, *, observed_ts: int, instance_id: str, epoch_id: str, incomplete: set | None = None) -> list:
    out = []
    for (sym, b), f in sorted(agg.items()):
        flags = ["DERIVED"] + (["INCOMPLETE_BUCKET"] if incomplete and (sym, b) in incomplete else [])
        r = C.make_record(symbol=sym, metric="trade_flow_1m", event_ts=b + BUCKET_MS, observed_ts=observed_ts,
                          value=f["total_notional"], units="quote_usdt", source_channel="derived:trade_events",
                          raw=f, instance_id=instance_id, epoch_id=epoch_id, flags=flags, payload=f)
        r["bucket_ts"] = b
        out.append(r)
    return out
