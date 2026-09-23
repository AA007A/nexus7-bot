"""Task-local raw KuCoin kline field provenance for runtime truth capture.

This module parses only telemetry copies of the exact REST response bytes already
read by the normal aiohttp response object. It has no exchange client imports and
performs no network I/O.
"""
from __future__ import annotations

import contextvars
import json

_raw_fields_by_ts = contextvars.ContextVar("bgx_truth_rest_raw_fields_by_ts", default=None)


def remember_raw_kline_fields(raw_body: bytes) -> dict[int, dict]:
    fields: dict[int, dict] = {}
    try:
        document = json.loads(raw_body.decode("utf-8", errors="strict"))
        rows = document.get("data", []) if isinstance(document, dict) else []
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            ts = int(float(row[0]))
            if ts < 100_000_000_000:
                ts *= 1000
            fields[ts] = {
                "candle_ts": ts,
                "rest_row_5_volume": row[5],
                "rest_row_6_turnover": row[6],
            }
    except Exception:
        fields = {}
    _raw_fields_by_ts.set(fields)
    return fields


def current_raw_kline_fields() -> dict[int, dict]:
    return {int(k): dict(v) for k, v in dict(_raw_fields_by_ts.get() or {}).items()}
