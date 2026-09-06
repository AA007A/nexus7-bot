"""Fail-closed guard for the legacy liquidation-safety override.

The execution engine contains a historical environment-controlled escape hatch
named ``ALLOW_SL_BEYOND_LIQUIDATION``. Allowing that flag in any runtime would
permit a new position whose configured stop can be reached only after the
exchange liquidation point. That is incompatible with the NEXUS-7 production
safety invariant.

This module is installed by ``sitecustomize``. A truthy override therefore
causes sitecustomize installation to fail, and main.py refuses to start the
trading engine. The guard does not change PAPER/LIVE selection, leverage,
sizing, credentials, order routing, or risk thresholds.
"""
from __future__ import annotations

import os


_UNSAFE_VAR = "ALLOW_SL_BEYOND_LIQUIDATION"


def install(log) -> None:
    raw = os.environ.get(_UNSAFE_VAR, "").strip().lower()
    if raw == "true":
        raise RuntimeError(
            "unsafe liquidation override is forbidden: "
            "ALLOW_SL_BEYOND_LIQUIDATION=true"
        )
    log.info(
        "[LIQUIDATION_GUARD] fail-closed: unsafe stop-beyond-liquidation "
        "override is disabled"
    )
