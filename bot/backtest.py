"""
KAKAZITO TRADE — Backtesting Engine
Busca dados históricos OHLCV da Bybit e roda as estratégias.
Calcula: Win Rate, Profit Factor, Sharpe, Sortino, Max DD,
         Expectancy, melhor/pior horário UTC, melhor/pior dia da semana,
         performance em trending vs ranging.
Roda automaticamente toda semana.
Persiste em tabela performance.
"""
import asyncio, time
from datetime import datetime, timezone
from typing import List, Dict
import numpy as np
from bot.logger import log


# ── Busca histórico OHLCV ────────────────────────────────────────
async def fetch_history(client, symbol: str, interval: str, limit: int = 1000) -> list:
    """Busca até 1000 candles históricos da Bybit."""
    try:
        kl = await client.get_klines(symbol, interval, limit)
        return kl
    except Exception as e:
        log.error(f"backtest fetch {symbol} {interval}: {e}")
        return []


# ── Simulador de estratégia MTF ──────────────────────────────────
def _run_strategy(klines_15: list, klines_1h: list, klines_4h: list,
                  min_score: int = 75) -> List[dict]:
    """
    Simula a estratégia MTF sobre dados históricos.
    Retorna lista de trades simulados.
    """
    from bot.strategy import Analyzer
    from bot.indicators import atr

    analyzer = Analyzer()
    trades   = []

    # Janela deslizante: usa 60 candles para análise, avança 1 a 1
    WINDOW = 60
    for i in range(WINDOW, len(klines_15) - 1):
        k15 = klines_15[max(0, i-WINDOW):i]
        k1h = klines_1h[max(0, i//4-20):i//4]   # approx
        k4h = klines_4h[max(0, i//16-15):i//16]

        if len(k15) < 30 or len(k1h) < 10 or len(k4h) < 5:
            continue

        try:
            sig = analyzer.analyze_mtf(
                "BT", k15, k1h, k4h,
                min_score=min_score,
                fee_mult=2.5,
                vol_mult=1.0,
            )
        except Exception:
            continue

        if not sig:
            continue

        # Simula resultado: verifica próximos 20 candles
        entry  = sig.entry
        sl     = sig.sl
        tp     = sig.tp
        opened = klines_15[i].get("c", entry)

        result = None
        hold   = 0
        for j in range(i+1, min(i+21, len(klines_15))):
            future = klines_15[j]
            hold  += 1
            if sig.direction == "LONG":
                if future["l"] <= sl:
                    result = "LOSS"; break
                if future["h"] >= tp:
                    result = "WIN";  break
            else:
                if future["h"] >= sl:
                    result = "LOSS"; break
                if future["l"] <= tp:
                    result = "WIN";  break

        if result is None:
            result = "TIMEOUT"

        # PnL estimado
        fee_pct = 0.0016
        if result == "WIN":
            pnl_pct = abs(tp - entry) / entry - fee_pct
        elif result == "LOSS":
            pnl_pct = -(abs(sl - entry) / entry) - fee_pct
        else:
            # Timeout: fechado no preço atual
            last   = klines_15[min(i+20, len(klines_15)-1)]["c"]
            pnl_pct= (last - entry) / entry * (1 if sig.direction == "LONG" else -1) - fee_pct

        # Timestamp do candle
        candle_idx = i
        hour_utc   = (candle_idx * 15 // 60) % 24
        day_of_week= (candle_idx // 96) % 7   # aprox

        trades.append({
            "result":     result,
            "pnl_pct":    pnl_pct,
            "hold":       hold,
            "direction":  sig.direction,
            "score":      sig.score,
            "hour_utc":   hour_utc,
            "day_of_week":day_of_week,
            "rr":         sig.rr,
        })

    return trades


# ── Métricas ─────────────────────────────────────────────────────
def _calc_metrics(trades: List[dict], strategy: str = "MTF") -> dict:
    if not trades:
        return {}

    pnls    = np.array([t["pnl_pct"] for t in trades])
    wins    = pnls[pnls > 0]
    losses  = pnls[pnls < 0]
    total   = len(pnls)
    win_rate= len(wins) / total * 100 if total else 0

    # Profit Factor
    gross_profit = wins.sum() if len(wins) else 0
    gross_loss   = abs(losses.sum()) if len(losses) else 1e-9
    pf = gross_profit / gross_loss

    # Sharpe
    sharpe = float(pnls.mean() / pnls.std()) if pnls.std() > 0 and total > 1 else 0

    # Sortino (só downside)
    neg_std = losses.std() if len(losses) > 1 else 1e-9
    sortino = float(pnls.mean() / neg_std) if neg_std > 0 else 0

    # Max Drawdown
    cum  = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    dd   = peak - cum
    max_dd = float(dd.max()) if len(dd) else 0

    # Expectancy
    expectancy = float(pnls.mean())

    # Melhor/pior hora UTC
    hour_pnl = {}
    for t in trades:
        h = t["hour_utc"]
        hour_pnl.setdefault(h, []).append(t["pnl_pct"])
    hour_avg = {h: np.mean(v) for h, v in hour_pnl.items()}
    best_hour  = max(hour_avg, key=hour_avg.get) if hour_avg else None
    worst_hour = min(hour_avg, key=hour_avg.get) if hour_avg else None

    # Melhor/pior dia da semana
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    day_pnl = {}
    for t in trades:
        d = t["day_of_week"]
        day_pnl.setdefault(d, []).append(t["pnl_pct"])
    day_avg  = {days[d]: np.mean(v) for d, v in day_pnl.items()}
    best_day  = max(day_avg, key=day_avg.get) if day_avg else None
    worst_day = min(day_avg, key=day_avg.get) if day_avg else None

    # Trending vs Ranging (usa score como proxy)
    trending = [t for t in trades if t.get("score", 0) >= 80]
    ranging  = [t for t in trades if t.get("score", 0) < 80]
    trending_wr = len([t for t in trending if t["pnl_pct"] > 0]) / len(trending) * 100 if trending else 0
    ranging_wr  = len([t for t in ranging  if t["pnl_pct"] > 0]) / len(ranging)  * 100 if ranging  else 0

    return {
        "strategy":            strategy,
        "total_trades":        total,
        "win_rate":            round(win_rate, 1),
        "profit_factor":       round(pf, 2),
        "sharpe_ratio":        round(sharpe, 3),
        "sortino_ratio":       round(sortino, 3),
        "max_drawdown_pct":    round(max_dd * 100, 2),
        "expectancy_pct":      round(expectancy * 100, 4),
        "avg_hold_candles":    round(np.mean([t["hold"] for t in trades]), 1),
        "best_hour_utc":       best_hour,
        "worst_hour_utc":      worst_hour,
        "best_day":            best_day,
        "worst_day":           worst_day,
        "trending_win_rate":   round(trending_wr, 1),
        "ranging_win_rate":    round(ranging_wr, 1),
        "gross_profit_pct":    round(float(gross_profit) * 100, 2),
        "gross_loss_pct":      round(float(gross_loss) * 100, 2),
    }


# ── Runner principal ─────────────────────────────────────────────
async def run_backtest(client, symbol: str = "BTCUSDT") -> dict:
    """Roda backtest completo e persiste resultado."""
    log.info(f"🔬 Iniciando backtest {symbol}...")
    start = time.time()

    k15 = await fetch_history(client, symbol, "15",  1000)
    k1h = await fetch_history(client, symbol, "60",  500)
    k4h = await fetch_history(client, symbol, "240", 300)

    if not k15:
        return {"error": "Sem dados históricos"}

    trades  = _run_strategy(k15, k1h, k4h)
    metrics = _calc_metrics(trades, "MTF-4H-1H-15M")

    elapsed = round(time.time() - start, 1)
    metrics["elapsed_seconds"] = elapsed
    metrics["symbol"]          = symbol
    metrics["candles_analyzed"]= len(k15)
    metrics["ran_at"]          = datetime.now(timezone.utc).isoformat()

    log.info(
        f"✅ Backtest {symbol} concluído em {elapsed}s | "
        f"{len(trades)} trades | WR={metrics.get('win_rate')}% | "
        f"PF={metrics.get('profit_factor')} | Sharpe={metrics.get('sharpe_ratio')}"
    )

    # Persiste no banco
    try:
        from bot import database as dbase
        await dbase._exec(
            """INSERT INTO performance
               (periodo,strategy,win_rate,profit_factor,sharpe_ratio,sortino_ratio,
                max_drawdown,expectancy_por_trade,total_trades,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).date().isoformat(),
                metrics["strategy"],
                metrics["win_rate"],
                metrics["profit_factor"],
                metrics["sharpe_ratio"],
                metrics["sortino_ratio"],
                metrics["max_drawdown_pct"] / 100,
                metrics["expectancy_pct"] / 100,
                metrics["total_trades"],
                metrics["ran_at"],
            ),
        )
    except Exception as e:
        log.error(f"backtest persist: {e}")

    return metrics


# ── Scheduler semanal ────────────────────────────────────────────
async def weekly_backtest_loop(client):
    """Roda backtest toda semana automaticamente."""
    WEEK = 7 * 24 * 3600
    while True:
        await asyncio.sleep(WEEK)
        try:
            for sym in ["BTCUSDT", "ETHUSDT"]:
                await run_backtest(client, sym)
                await asyncio.sleep(60)
        except Exception as e:
            log.error(f"weekly_backtest: {e}")
