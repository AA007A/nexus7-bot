"""R:R boundary precision hardening.

SL/TP are rounded to 6 decimals before the ratio is checked. For low-priced
contracts this can turn an intended exact 2.0000 geometry into 1.9999... and
make the strict `rr < 2.0` comparison reject a setup while the log prints 2.00.

The previous workaround temporarily mutated the shared cfg.MIN_RR_RATIO value.
That was restored in finally, but another coroutine/module could still observe
the lowered threshold during Analyzer execution. This implementation instead
installs a strategy-local config proxy for the duration of the call. The shared
config object itself is never mutated.

Logical minimum R:R, strategy weights, NEXUS gate, risk settings and LIVE/PAPER
mode are unchanged.
"""
from __future__ import annotations

import threading

_RR_EPSILON = 1e-4
_ANALYZE_LOCK = threading.RLock()


class _CfgProxy:
    """Read-through cfg view overriding only MIN_RR_RATIO."""

    __slots__ = ("_base", "MIN_RR_RATIO")

    def __init__(self, base, min_rr_ratio: float):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "MIN_RR_RATIO", min_rr_ratio)

    def __getattr__(self, name):
        return getattr(self._base, name)

    def __setattr__(self, name, value):
        raise AttributeError(f"RR precision cfg proxy is read-only: {name}")


def install(log):
    import bot.strategy as strategy

    Analyzer = strategy.Analyzer
    if getattr(Analyzer, "_rr_precision_patched", False):
        return

    original_analyze = Analyzer.analyze_mtf

    def analyze_with_rr_precision(self, *args, **kwargs):
        # Serialize Analyzer calls while its module-local cfg binding is
        # temporarily replaced. bot.config.cfg and every other module keep the
        # original configured threshold throughout the call.
        with _ANALYZE_LOCK:
            original_cfg = strategy.cfg
            configured = float(original_cfg.MIN_RR_RATIO)
            effective = max(0.0, configured - _RR_EPSILON)
            strategy.cfg = _CfgProxy(original_cfg, effective)
            try:
                result = original_analyze(self, *args, **kwargs)
            finally:
                strategy.cfg = original_cfg

        if result is not None:
            actual_rr = float(getattr(result, "rr", 0.0) or 0.0)
            delta = configured - actual_rr
            if 0.0 < delta <= (_RR_EPSILON + 1e-12):
                log.info(
                    "[RR_PRECISION] %s accepted at rounded R:R %.6f for logical minimum %.6f; quantization-only tolerance",
                    getattr(result, "symbol", "?"), actual_rr, configured,
                )
        return result

    Analyzer.analyze_mtf = analyze_with_rr_precision
    Analyzer._rr_precision_patched = True
    log.info(
        "[RR_PRECISION] strategy-local 6-decimal boundary tolerance enabled; shared config unchanged"
    )
