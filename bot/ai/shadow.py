"""SHADOW operation: full AI + deterministic chain decisions, ZERO orders.

Champion / challenger: both authorities see the SAME observation; only the
champion's intent is recorded as "would send"; neither can send anything.
Every decision (approved or not) is journaled. Latency percentiles cover the
full path features -> model -> chain -> order intent.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from bot.ai import authority_chain as ac
from bot.ai.execution_mode import ShadowAdapter
from bot.ai.journal import DecisionJournal


def percentiles(xs) -> dict:
    if not xs:
        return {"n": 0, "p50": None, "p95": None, "p99": None}
    s = sorted(xs)

    def q(p):
        return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]
    return {"n": len(s), "p50": q(0.50), "p95": q(0.95), "p99": q(0.99)}


@dataclass
class ShadowRunner:
    champion: object
    challenger: object | None = None
    journal: DecisionJournal = field(default_factory=DecisionJournal)
    adapter: ShadowAdapter = field(default_factory=ShadowAdapter)
    latencies_ms: list = field(default_factory=list)
    counts: dict = field(default_factory=lambda: {"LONG": 0, "SHORT": 0, "ABSTAIN": 0, "HOLD": 0})

    def observe(self, *, symbol, direction, features, regime, geometry, risk_ctx, halt, fees_r,
                slippage_r, funding_r=0.0, now_ms=None):
        t0 = time.perf_counter()
        results = {}
        for role, auth in (("CHAMPION", self.champion), ("CHALLENGER", self.challenger)):
            if auth is None:
                continue
            d = auth.decide(symbol=symbol, direction=direction, features=features, regime=regime,
                            fees_r=fees_r, slippage_r=slippage_r, funding_r=funding_r, now_ms=now_ms)
            chain = ac.run(d, geometry, risk_ctx, halted=halt.halted, halt_reasons=sorted(halt.active))
            if role == "CHAMPION":
                self.counts[d.side] = self.counts.get(d.side, 0) + 1
                if chain.approved:
                    self.adapter.submit(chain.intent)
            self.journal.append(decision={**d.to_dict(), "role": role},
                                feature_snapshot={"values": features.values, "missing": list(features.missing),
                                                  "schema_sha256": features.schema_sha256},
                                chain={"approved": chain.approved, "stage": chain.stage,
                                       "vetoes": chain.vetoes,
                                       "client_oid": chain.intent.client_oid if chain.intent else None})
            results[role] = chain
        self.latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        return results

    def report(self) -> dict:
        return {"decisions": dict(self.counts), "orders_sent": self.adapter.orders_sent,
                "would_send": len(self.adapter.recorded), "latency_ms": percentiles(self.latencies_ms),
                "journal_records": len(self.journal.records), "journal_intact": self.journal.verify()}
