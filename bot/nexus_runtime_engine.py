"""Explicit runtime TradingEngine composition for NEXUS-7.

The large canonical engine remains in ``bot.engine``. This runtime subclass
adds post-decision observability declaratively instead of replacing methods at
startup. All safety/risk hardenings installed on the canonical base class are
inherited normally.
"""
from __future__ import annotations

from bot.engine import TradingEngine as CoreTradingEngine
from bot.nexus_validation_observability import observe_nexus_validation


class TradingEngine(CoreTradingEngine):
    """Canonical engine plus result-preserving NEXUS decision telemetry."""

    @observe_nexus_validation
    async def _nexus_validate(self, sig):
        return await super()._nexus_validate(sig)
