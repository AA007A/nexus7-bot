"""Pre-trade score consistency hardening.

The production MTF strategy deliberately excludes the still-open candle before
scoring.  The legacy pre-trade scorer was receiving the raw cache including
that candle, so two gates could evaluate different market states.  This module
keeps the pre-trade gate and threshold intact while making its candle input
consistent with the strategy and exposing component details for auditability.
"""


def install(log):
    from bot import score as scoring

    if getattr(scoring, "_closed_candle_consistency_patched", False):
        return

    original_calculate = scoring.calculate

    async def calculate_closed_candles(
        symbol, direction, closes, highs, lows, volumes, client=None
    ):
        # Engine cache convention: the final 15m candle is still forming.
        # Match Analyzer.analyze_mtf(): when there is sufficient history,
        # exclude that candle so the pre-trade gate cannot repaint intrabar.
        if (
            len(closes) > 20
            and len(highs) == len(closes)
            and len(lows) == len(closes)
            and len(volumes) == len(closes)
        ):
            closes = closes[:-1]
            highs = highs[:-1]
            lows = lows[:-1]
            volumes = volumes[:-1]

        result = await original_calculate(
            symbol, direction, closes, highs, lows, volumes, client
        )

        try:
            if not result.get("aprovado", False):
                detail = result.get("detalhes", {}) or {}
                log.info(
                    "[PRETRADE_DETAIL] %s %s total=%s TEC=%s OF=%s MAC=%s NEWS=%s technical=%s orderflow=%s macro=%s",
                    symbol,
                    direction,
                    result.get("total"),
                    result.get("tecnico"),
                    result.get("orderflow"),
                    result.get("macro"),
                    result.get("news_mod"),
                    detail.get("tecnico", {}),
                    detail.get("orderflow", {}),
                    detail.get("macro", {}),
                )
        except Exception:
            pass

        return result

    scoring.calculate = calculate_closed_candles
    scoring._closed_candle_consistency_patched = True
    log.info(
        "[PRETRADE] closed-candle consistency enabled; threshold/gates unchanged"
    )
