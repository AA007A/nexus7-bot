"""One canonical execution mode: RESEARCH | SHADOW | PAPER | LIVE.

  RESEARCH  offline replay only; no adapter.
  SHADOW    full AI + risk decisions; send_order is impossible (records only).
  PAPER     simulated fills with production costs, lot/tick quantization,
            minimum-lot rejection; zero real orders.
  LIVE      real exchange adapter. Fails closed unless the Stage-C
            LIVE_RELEASE_GATE passed on SOURCE-AUTHENTICATED evidence. A local
            environment variable alone can never authorize real-money trading.

PAPER and LIVE share every upstream step; they diverge only at the adapter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

MODES = ("RESEARCH", "SHADOW", "PAPER", "LIVE")


class LiveNotAuthorized(PermissionError):
    pass


def resolve_mode(env: dict, *, stage_c_result=None) -> str:
    """EXECUTION_MODE from the environment; LIVE additionally requires a
    GateResult from nexus_oos_promotion_gate.evaluate_live that passed on
    source-authenticated evidence. Unknown values fall back to SHADOW."""
    mode = str((env or {}).get("EXECUTION_MODE", "SHADOW")).strip().upper()
    if mode not in MODES:
        return "SHADOW"
    if mode == "LIVE":
        d = stage_c_result.to_dict() if hasattr(stage_c_result, "to_dict") else None
        if not (d and d.get("gate") == "LIVE_RELEASE_GATE" and d.get("verdict") == "PASS"
                and d.get("live_provenance_authenticated") is True
                and d.get("production_ready") is True):
            raise LiveNotAuthorized("LIVE requires a passing Stage-C LIVE_RELEASE_GATE "
                                    "on source-authenticated evidence")
    return mode


@dataclass
class ShadowAdapter:
    """Records what would have been sent. Cannot send anything."""
    mode: str = "SHADOW"
    recorded: list = field(default_factory=list)
    orders_sent: int = 0

    def submit(self, intent) -> dict:
        self.recorded.append({"client_oid": intent.client_oid, "side": intent.side, "qty": intent.qty,
                              "entry": intent.entry})
        return {"status": "NOT_SENT_SHADOW", "client_oid": intent.client_oid}


@dataclass
class PaperAdapter:
    """Simulated execution with production costs. Never touches an exchange."""
    taker_fee: float
    slippage_rate: float
    tick_size: float
    lot_base: float
    min_base: float
    mode: str = "PAPER"
    fills: dict = field(default_factory=dict)
    orders_sent: int = 0          # real orders: always 0

    def _q(self, x: float, step: float, down: bool = True) -> float:
        d = (Decimal(str(x)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)
        return float(d * Decimal(str(step)))

    def submit(self, intent, *, best_bid: float, best_ask: float) -> dict:
        if intent.client_oid in self.fills:            # idempotent: same intent -> same fill
            return {**self.fills[intent.client_oid], "duplicate": True}
        qty = self._q(intent.qty, self.lot_base)
        if qty < self.min_base or qty <= 0:
            out = {"status": "REJECTED_MIN_LOT", "client_oid": intent.client_oid}
            self.fills[intent.client_oid] = out
            return out
        ref = best_ask if intent.side == "LONG" else best_bid
        px = ref * (1 + self.slippage_rate) if intent.side == "LONG" else ref * (1 - self.slippage_rate)
        px = self._q(px, self.tick_size) if intent.side == "SHORT" else \
            math.ceil(px / self.tick_size) * self.tick_size
        out = {"status": "FILLED", "client_oid": intent.client_oid, "qty": qty, "price": px,
               "fee": qty * px * self.taker_fee}
        self.fills[intent.client_oid] = out
        return out


@dataclass
class LiveAdapterGuard:
    """Wraps a real sender; refuses to exist without Stage-C authorization."""
    sender: object
    stage_c_result: object
    mode: str = "LIVE"

    def __post_init__(self):
        resolve_mode({"EXECUTION_MODE": "LIVE"}, stage_c_result=self.stage_c_result)
