"""Validation contract for a controlled exchange E2E lifecycle.

This module validates evidence produced by a future controlled run. It never
places, cancels, amends or closes an exchange order itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


_REQUIRED = (
    "PRE_DISPATCH_EXPOSURE_CLEAR",
    "MICROSTRUCTURE_PASS",
    "DURABLE_INTENT_PERSISTED",
    "ORDER_ACK",
    "FILL_CONFIRMED",
    "PROTECTION_CONFIRMED",
    "POSITION_RECONCILED",
    "EXIT_CONFIRMED",
    "FINAL_EXPOSURE_CLEAR",
)


@dataclass(frozen=True)
class E2EValidationResult:
    passed: bool
    missing: tuple[str, ...]
    out_of_order: tuple[str, ...]


def validate_e2e_evidence(events: Iterable[str]) -> E2EValidationResult:
    observed = [str(e) for e in events]
    missing = tuple(e for e in _REQUIRED if e not in observed)
    out_of_order: list[str] = []
    last_index = -1
    for required in _REQUIRED:
        if required not in observed:
            continue
        idx = observed.index(required)
        if idx <= last_index:
            out_of_order.append(required)
        last_index = max(last_index, idx)
    return E2EValidationResult(not missing and not out_of_order, missing, tuple(out_of_order))


def required_evidence() -> tuple[str, ...]:
    return _REQUIRED
