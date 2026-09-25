"""Level-2 local book with strict sequence integrity (BOOK_POLICY)."""
from __future__ import annotations

from decimal import Decimal

INIT, SYNCING, VALID, INVALID = "INIT", "SYNCING", "VALID", "INVALID"


class OrderBook:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.state = INIT
        self.bids: dict = {}
        self.asks: dict = {}
        self.seq = None
        self.buffer: list = []
        self.stats = {"applied": 0, "duplicates": 0, "gaps": 0, "resyncs": 0, "out_of_order": 0}
        self.last_event_ts = None
        self.invalid_since = None

    # ── lifecycle ──
    def begin_sync(self):
        """Subscription is live; buffer deltas until the snapshot arrives."""
        self.state, self.buffer = SYNCING, []

    def invalidate(self, ts=None, reason="GAP"):
        if self.state != INVALID:
            self.invalid_since = ts
        self.state = INVALID
        self.buffer = []
        return reason

    def on_delta(self, seq: int, price: str, side: str, size: str, ts: int | None = None) -> str:
        """Returns APPLIED / BUFFERED / DUPLICATE / GAP / IGNORED_INVALID."""
        seq = int(seq)
        if self.state == SYNCING:
            self.buffer.append((seq, price, side, size, ts))
            return "BUFFERED"
        if self.state in (INIT, INVALID):
            return "IGNORED_INVALID"
        if seq <= self.seq:
            self.stats["duplicates" if seq == self.seq or self._seen(seq) else "out_of_order"] += 1
            return "DUPLICATE"
        if seq != self.seq + 1:
            self.stats["gaps"] += 1
            self.invalidate(ts)
            return "GAP"
        self._apply(price, side, size)
        self.seq = seq
        self.last_event_ts = ts if ts is not None else self.last_event_ts
        self.stats["applied"] += 1
        return "APPLIED"

    def _seen(self, seq):
        return seq <= self.seq

    def on_snapshot(self, seq: int, bids, asks, ts: int | None = None) -> str:
        """REST snapshot: replace book, then apply buffered deltas with seq > snapshot seq (contiguously)."""
        self.bids = {Decimal(str(p)): Decimal(str(s)) for p, s in bids if Decimal(str(s)) > 0}
        self.asks = {Decimal(str(p)): Decimal(str(s)) for p, s in asks if Decimal(str(s)) > 0}
        self.seq = int(seq)
        buffered = sorted(self.buffer)
        self.buffer = []
        self.state = VALID
        self.last_event_ts = ts
        self.stats["resyncs"] += 1
        for d in buffered:
            if d[0] <= self.seq:
                continue
            if self.on_delta(*d) == "GAP":
                return INVALID
        self.invalid_since = None
        return self.state

    def _apply(self, price, side, size):
        book = self.bids if str(side).lower() in ("buy", "bid") else self.asks
        p, s = Decimal(str(price)), Decimal(str(size))
        if s == 0:
            book.pop(p, None)
        else:
            book[p] = s

    # ── features (only when VALID) ──
    def features(self) -> dict | None:
        if self.state != VALID or not self.bids or not self.asks:
            return None
        bids = sorted(self.bids.items(), key=lambda kv: -kv[0])
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])
        (b1, bs1), (a1, as1) = bids[0], asks[0]
        if b1 >= a1:
            return None                                                   # crossed book -> not emitted
        out = {"bid1": b1, "ask1": a1, "bid1_size": bs1, "ask1_size": as1, "spread": a1 - b1, "mid": (a1 + b1) / 2,
               "microprice": (b1 * as1 + a1 * bs1) / (bs1 + as1)}
        for n in (5, 10):
            db = sum((s for _, s in bids[:n]), Decimal(0))
            da = sum((s for _, s in asks[:n]), Decimal(0))
            out[f"depth_bid_{n}"], out[f"depth_ask_{n}"] = db, da
            out[f"imbalance_{n}"] = (db - da) / (db + da) if (db + da) > 0 else None
        out["levels_bid"], out["levels_ask"] = len(bids), len(asks)
        return {k: (None if v is None else str(v)) for k, v in out.items()}
