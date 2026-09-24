"""Append-only AI decision journal (every decision, including rejections).

Each record is hash-chained (``prev_sha256``) so edits or deletions are
detectable, and carries everything needed to reproduce the decision: model
sha, feature schema/hash, the raw feature snapshot, AI outputs, regime,
reason codes, risk veto, final decision, and (when traded) order ids, fills,
fees, slippage, funding, realized R, MAE, MFE and exit reason.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

GENESIS = "0" * 64


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


class DecisionJournal:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.records: list[dict] = []
        if self.path and self.path.exists():
            self.records = [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    @property
    def head(self) -> str:
        return self.records[-1]["record_sha256"] if self.records else GENESIS

    def append(self, *, decision: dict, feature_snapshot: dict, chain: dict, outcome: dict | None = None) -> dict:
        body = {"decision": decision, "feature_snapshot": feature_snapshot, "chain": chain,
                "outcome": outcome or {}, "prev_sha256": self.head}
        rec = {**body, "record_sha256": _sha(body)}
        self.records.append(rec)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        return rec

    def verify(self) -> bool:
        prev = GENESIS
        for rec in self.records:
            body = {k: v for k, v in rec.items() if k != "record_sha256"}
            if rec.get("prev_sha256") != prev or _sha(body) != rec.get("record_sha256"):
                return False
            prev = rec["record_sha256"]
        return True
