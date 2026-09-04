"""R:R boundary precision hardening.

SL/TP are rounded to 6 decimals before the ratio is checked. For low-priced
contracts this can turn an intended exact 2.0000 geometry into 1.9999... and
make the strict `rr < 2.0` comparison reject a setup while the log prints 2.00.

This wrapper applies only a 1e-4 numerical tolerance during Analyzer execution,
then immediately restores the configured threshold. It does not change the
logical minimum R:R, strategy weights, NEXUS gate, risk settings, or LIVE/PAPER
mode.
"""


def install(log):
    from bot.strategy import Analyzer
    from bot.config import cfg

    if getattr(Analyzer, "_rr_precision_patched", False):
        return

    original_analyze = Analyzer.analyze_mtf

    def analyze_with_rr_precision(self, *args, **kwargs):
        configured = float(cfg.MIN_RR_RATIO)
        # Six-decimal SL/TP quantization can perturb the computed ratio by a
        # few 1e-5 on low-priced assets. 1e-4 is deliberately smaller than
        # any economically meaningful threshold change.
        cfg.MIN_RR_RATIO = configured - 0.0001
        try:
            result = original_analyze(self, *args, **kwargs)
        finally:
            cfg.MIN_RR_RATIO = configured

        if result is not None:
            actual_rr = float(getattr(result, "rr", 0.0) or 0.0)
            if actual_rr < configured and configured - actual_rr <= 0.01:
                log.info(
                    "[RR_PRECISION] %s accepted at rounded R:R %.4f for logical minimum %.4f; quantization-only tolerance",
                    getattr(result, "symbol", "?"), actual_rr, configured,
                )
        return result

    Analyzer.analyze_mtf = analyze_with_rr_precision
    Analyzer._rr_precision_patched = True
    log.info("[RR_PRECISION] 6-decimal boundary tolerance enabled; logical min R:R unchanged")
