"""Validation contract for a controlled exchange E2E lifecycle.

This module validates evidence produced by a future controlled run. It never
places, cancels, amends or closes an exchange order itself.

It is also an explicit read-only CLI entrypoint:

    python -m bot.e2e_validation_contract --required
    python -m bot.e2e_validation_contract EVENT1 EVENT2 ...

The CLI only validates supplied evidence strings and returns an exit status.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Iterable, Sequence


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


def main(argv: Sequence[str] | None = None) -> int:
    """Validate supplied evidence without any exchange or runtime mutation."""
    parser = argparse.ArgumentParser(
        description="Validate NEXUS-7 controlled E2E evidence ordering."
    )
    parser.add_argument(
        "events",
        nargs="*",
        help="Observed evidence event names in chronological order.",
    )
    parser.add_argument(
        "--required",
        action="store_true",
        help="Print the required evidence sequence and exit successfully.",
    )
    args = parser.parse_args(argv)

    if args.required:
        print(json.dumps({
            "required": list(required_evidence()),
            "decision_effect": "NONE",
            "execution_effect": "NONE",
        }, sort_keys=True))
        return 0

    result = validate_e2e_evidence(args.events)
    print(json.dumps({
        "passed": result.passed,
        "missing": list(result.missing),
        "out_of_order": list(result.out_of_order),
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
