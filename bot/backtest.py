"""
BGX Capital — Backtesting Engine
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
from bot.config import cfg


# ── Busca histórico OHLCV ────────────────────────────────────────
async def fetch_history(client, symbol: str, interval: str, limit: int = 1000) -> list:
    """
    Busca histórico OHLCV da Bybit em lotes de 1000 candles.
    Para 90 dias no 15M: ~8640 candles → 9 lotes automáticos.
    Para 90 dias no  1H: ~2160 candles → 3 lotes.
    Para 90 dias no  4H: ~540  candles → 1 lote.
    """
    try:
        if limit <= 1000:
            # Busca simples para quantidades pequenas
            return await client.get_klines(symbol, interval, limit)

        # Busca em lotes de 1000 para quantidades maiores
        all_klines = []
        remaining  = limit
        while remaining > 0:
            batch_size = min(1000, remaining)
            try:
                batch = await client.get_klines(symbol, interval, batch_size)
            except Exception as e:
                log.warning(f"backtest fetch lote {symbol} {interval}: {e}")
                break
            if not batch:
                break
            # Evita duplicatas: descarta candles que já temos
            if all_klines:
                known_open = {k["o"] for k in all_klines[-5:]}
                batch = [k for k in batch if k["o"] not in known_open]
            all_klines.extend(batch)
            remaining -= len(batch)
            if len(batch) < batch_size:
                break   # exchange retornou menos que pedido = sem mais dados
            await asyncio.sleep(0.3)   # respeita rate limit

        log.info(f"fetch_history {symbol} {interval}: {len(all_klines)} candles")
        return all_klines
    except Exception as e:
        log.error(f"backtest fetch {symbol} {interval}: {e}")
        return []


# ── Simulador de estratégia MTF ──────────────────────────────────
def _run_strategy(klines_15: list, klines_1h: list, klines_4h: list,
                  min_score: int = 75,
                  min_rr: float = 2.0) -> List[dict]:
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
                min_score=getattr(cfg, 'MIN_ENTRY_SCORE', min_score),
                fee_mult=getattr(cfg, 'FEE_MULTIPLIER', 2.0),
                vol_mult=getattr(cfg, 'MIN_VOLUME_MULT', 1.2),
            )
        except Exception:
            continue

        if not sig:
            continue
        if sig.rr < getattr(cfg, "MIN_RR_RATIO", min_rr):
            continue

        # Simula resultado: verifica próximos 20 candles
        entry  = sig.entry
        sl     = sig.sl
        tp     = sig.tp
        opened = klines_15[i].get("c", entry)

        # ── Simula TP parcial 50%/50% ────────────────────────────
        tp1 = getattr(sig, 'tp1', tp)
        tp2 = getattr(sig, 'tp2', tp)
        # Se tp1 == tp2 (sem TP parcial definido), usa TP único
        has_partial = tp1 != tp2 and tp1 != 0

        result    = None
        hold      = 0
        tp1_hit   = False
        pnl_pct   = 0.0
        fee_pct   = 0.0016   # 0.16% total (entrada + saída)

        for j in range(i+1, min(i+41, len(klines_15))):  # até 40 candles (~10h)
            future = klines_15[j]
            hold  += 1

            if sig.direction == "LONG":
                # SL atingido
                if future["l"] <= sl:
                    if tp1_hit:
                        # Metade já garantida — SL está em break-even
                        pnl_tp1  = abs(tp1 - entry) / entry * 0.5
                        pnl_sl   = 0.0   # SL = break-even, sem perda adicional
                        pnl_pct  = pnl_tp1 + pnl_sl - fee_pct
                        result   = "PARTIAL_WIN"
                    else:
                        pnl_pct = -(abs(sl - entry) / entry) - fee_pct
                        result  = "LOSS"
                    break
                # TP1 atingido (50%)
                if has_partial and not tp1_hit and future["h"] >= tp1:
                    tp1_hit = True
                    sl      = entry   # move SL para break-even
                # TP2 atingido (50% restante)
                if tp1_hit and future["h"] >= tp2:
                    pnl_tp1 = abs(tp1 - entry) / entry * 0.5
                    pnl_tp2 = abs(tp2 - entry) / entry * 0.5
                    pnl_pct = pnl_tp1 + pnl_tp2 - fee_pct
                    result  = "WIN"; break
                # TP único (sem parcial)
                if not has_partial and future["h"] >= tp:
                    pnl_pct = abs(tp - entry) / entry - fee_pct
                    result  = "WIN"; break

            else:  # SHORT
                if future["h"] >= sl:
                    if tp1_hit:
                        pnl_tp1 = abs(tp1 - entry) / entry * 0.5
                        pnl_pct = pnl_tp1 - fee_pct
                        result  = "PARTIAL_WIN"
                    else:
                        pnl_pct = -(abs(sl - entry) / entry) - fee_pct
                        result  = "LOSS"
                    break
                if has_partial and not tp1_hit and future["l"] <= tp1:
                    tp1_hit = True
                    sl      = entry
                if tp1_hit and future["l"] <= tp2:
                    pnl_tp1 = abs(tp1 - entry) / entry * 0.5
                    pnl_tp2 = abs(tp2 - entry) / entry * 0.5
                    pnl_pct = pnl_tp1 + pnl_tp2 - fee_pct
                    result  = "WIN"; break
                if not has_partial and future["l"] <= tp:
                    pnl_pct = abs(tp - entry) / entry - fee_pct
                    result  = "WIN"; break

        if result is None:
            result = "TIMEOUT"
            last    = klines_15[min(i+40, len(klines_15)-1)]["c"]
            base    = (last - entry) / entry * (1 if sig.direction == "LONG" else -1)
            pnl_pct = (base * (0.5 if tp1_hit else 1.0)) - fee_pct

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
    # PARTIAL_WIN conta como win nas métricas
    wins    = pnls[pnls > 0]
    losses  = pnls[pnls < 0]
    total   = len(pnls)
    partial = [t for t in trades if t.get("result") == "PARTIAL_WIN"]
    win_rate= (len(wins) + len(partial) * 0.5) / total * 100 if total else 0

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




# Alias público para uso externo (optimizer, testes)
run_strategy_public = _run_strategy


# ── Monte Carlo Permutation Test ─────────────────────────────────
def monte_carlo_permutation(returns: list, n_simulations: int = 5000,
                             random_seed: int = 42) -> dict:
    """
    Valida estatisticamente se o edge da estratégia é REAL ou fruto de sorte.

    Metodologia:
      1. Calcula o Sharpe ratio da estratégia real
      2. Embaralha aleatoriamente os retornos N vezes (5000 por padrão)
      3. Calcula o Sharpe de cada versão aleatória
      4. p-value = % das versões aleatórias que batem ou igualam o Sharpe real

    Interpretação do p-value:
      < 0.01 → edge MUITO significativo (99% confiança) — verde
      < 0.05 → edge significativo       (95% confiança) — aceitável
      < 0.10 → edge fraco               (90% confiança) — cuidado
      >= 0.10 → SEM EDGE estatístico    — NÃO operar com capital real

    Retorna também:
      sharpe_percentile: onde o Sharpe real está na distribuição aleatória
      min_trades_needed: mínimo de trades para resultado ser confiável
    """
    if not returns or len(returns) < 10:
        return {"error": "Mínimo 10 trades para Monte Carlo", "edge_significant": False}

    arr = np.array(returns, dtype=float)
    np.random.seed(random_seed)

    # Sharpe da estratégia real
    std = arr.std()
    real_sharpe = float(arr.mean() / std) if std > 0 else 0.0

    # Simulações aleatórias
    random_sharpes = np.zeros(n_simulations)
    for i in range(n_simulations):
        shuffled = np.random.permutation(arr)
        s = shuffled.std()
        random_sharpes[i] = float(shuffled.mean() / s) if s > 0 else 0.0

    # p-value: fração das simulações >= Sharpe real
    p_value = float((random_sharpes >= real_sharpe).mean())

    # Percentil: onde o Sharpe real está na distribuição
    percentile = float((random_sharpes < real_sharpe).mean() * 100)

    # Classificação do nível de confiança
    if p_value < 0.01:
        confidence = "VERY_HIGH"
        verdict    = "Edge estatisticamente muito significativo (99%+)"
    elif p_value < 0.05:
        confidence = "HIGH"
        verdict    = "Edge estatisticamente significativo (95%+)"
    elif p_value < 0.10:
        confidence = "MEDIUM"
        verdict    = "Edge fraco — monitorar com cautela (90%)"
    else:
        confidence = "NONE"
        verdict    = "SEM EDGE estatístico — resultados consistentes com sorte"

    # Mínimo de trades necessários para 95% de confiança
    # Regra empírica: N > (Z/margin_of_error)² onde Z=1.96 para 95%
    win_rate_est = float((arr > 0).mean())
    if 0 < win_rate_est < 1:
        margin = 0.05   # ±5% de margem de erro no win rate
        min_trades = int(np.ceil((1.96 ** 2 * win_rate_est * (1 - win_rate_est)) / (margin ** 2)))
    else:
        min_trades = 100  # fallback conservador

    return {
        "real_sharpe":        round(real_sharpe, 4),
        "p_value":            round(p_value, 4),
        "confidence_level":   confidence,
        "verdict":            verdict,
        "edge_significant":   p_value < 0.05,
        "sharpe_percentile":  round(percentile, 1),
        "random_sharpe_mean": round(float(random_sharpes.mean()), 4),
        "random_sharpe_std":  round(float(random_sharpes.std()), 4),
        "n_simulations":      n_simulations,
        "n_trades":           len(returns),
        "min_trades_needed":  min_trades,
    }

# ── Walk-Forward Testing ─────────────────────────────────────────
def _walk_forward(klines_15: list, klines_1h: list, klines_4h: list,
                  n_windows: int = 3, train_ratio: float = 0.70) -> dict:
    """
    Walk-Forward Testing: divide os dados em N janelas temporais.
    Em cada janela: treina nos primeiros 70% e testa nos 30% restantes.
    Detecta degradação de performance entre treino e teste (overfitting).

    Retorna:
      windows:         métricas de cada janela (treino e teste)
      oos_win_rate:    win rate médio out-of-sample
      oos_pf:          profit factor médio out-of-sample
      degradation_pct: quanto a performance cai do treino para o teste (%)
      overfit_risk:    "LOW" | "MEDIUM" | "HIGH"
    """
    if len(klines_15) < 200:
        return {"error": "Dados insuficientes para walk-forward (min 200 candles 15M)"}

    n = len(klines_15)
    window_size = n // n_windows
    results = []

    for w in range(n_windows):
        start_idx = w * window_size
        end_idx   = start_idx + window_size if w < n_windows - 1 else n
        w_k15     = klines_15[start_idx:end_idx]

        # Índices proporcionais para 1H e 4H
        s1h = start_idx // 4
        e1h = end_idx   // 4
        s4h = start_idx // 16
        e4h = end_idx   // 16
        w_k1h = klines_1h[s1h:e1h] if e1h <= len(klines_1h) else klines_1h[s1h:]
        w_k4h = klines_4h[s4h:e4h] if e4h <= len(klines_4h) else klines_4h[s4h:]

        if len(w_k15) < 60:
            continue

        # Divide em treino e teste
        split      = int(len(w_k15) * train_ratio)
        train_15   = w_k15[:split]
        test_15    = w_k15[split:]
        split_1h   = split // 4
        split_4h   = split // 16
        train_1h   = w_k1h[:split_1h] if split_1h < len(w_k1h) else w_k1h
        train_4h   = w_k4h[:split_4h] if split_4h < len(w_k4h) else w_k4h
        test_1h    = w_k1h[split_1h:] if split_1h < len(w_k1h) else []
        test_4h    = w_k4h[split_4h:] if split_4h < len(w_k4h) else []

        # Roda estratégia em treino e teste
        train_trades = _run_strategy(train_15, train_1h, train_4h) if len(train_15) >= 60 else []
        test_trades  = _run_strategy(test_15,  test_1h,  test_4h)  if len(test_15)  >= 30 else []

        train_m = _calc_metrics(train_trades, "train") if train_trades else {}
        test_m  = _calc_metrics(test_trades,  "test")  if test_trades  else {}

        results.append({
            "window":        w + 1,
            "candles_train": len(train_15),
            "candles_test":  len(test_15),
            "train": {
                "win_rate":      train_m.get("win_rate", 0),
                "profit_factor": train_m.get("profit_factor", 0),
                "sharpe":        train_m.get("sharpe_ratio", 0),
                "total_trades":  train_m.get("total_trades", 0),
            },
            "test": {
                "win_rate":      test_m.get("win_rate", 0),
                "profit_factor": test_m.get("profit_factor", 0),
                "sharpe":        test_m.get("sharpe_ratio", 0),
                "total_trades":  test_m.get("total_trades", 0),
            },
        })

    if not results:
        return {"error": "Nenhuma janela com dados suficientes"}

    # Métricas agregadas out-of-sample
    oos_wr = float(np.mean([r["test"]["win_rate"]      for r in results if r["test"]["total_trades"] > 0] or [0]))
    oos_pf = float(np.mean([r["test"]["profit_factor"] for r in results if r["test"]["total_trades"] > 0] or [0]))
    is_wr  = float(np.mean([r["train"]["win_rate"]     for r in results if r["train"]["total_trades"] > 0] or [0]))
    is_pf  = float(np.mean([r["train"]["profit_factor"]for r in results if r["train"]["total_trades"] > 0] or [0]))

    # Degradação: quanto cai do treino para o teste
    wr_degradation  = round((is_wr - oos_wr) / max(is_wr, 1) * 100, 1)
    pf_degradation  = round((is_pf - oos_pf) / max(is_pf, 0.01) * 100, 1)
    avg_degradation = (wr_degradation + pf_degradation) / 2

    # Classificação de risco de overfitting
    if avg_degradation > 40:
        overfit_risk = "HIGH"    # estratégia overfitada nos dados de treino
    elif avg_degradation > 20:
        overfit_risk = "MEDIUM"  # alguma degradação, monitor
    else:
        overfit_risk = "LOW"     # robusta — performance se mantém fora da amostra

    return {
        "windows":         results,
        "oos_win_rate":    round(oos_wr, 1),
        "oos_pf":          round(oos_pf, 2),
        "is_win_rate":     round(is_wr, 1),
        "is_pf":           round(is_pf, 2),
        "wr_degradation_pct":  wr_degradation,
        "pf_degradation_pct":  pf_degradation,
        "overfit_risk":    overfit_risk,
        "n_windows":       n_windows,
    }


# ── Runner principal ─────────────────────────────────────────────
async def run_backtest(client, symbol: str = "BTCUSDT") -> dict:
    """
    Backtest completo com 90 dias de dados históricos + walk-forward testing.

    Coleta:
      15M: ~8640 candles (90 dias × 96 candles/dia)
       1H: ~2160 candles (90 dias × 24 candles/dia)
       4H:  ~540 candles (90 dias ×  6 candles/dia)

    Walk-forward: 3 janelas de 30 dias cada
      Treino: primeiros 70% da janela (21 dias)
      Teste:  últimos 30% da janela (9 dias)
    """
    log.info(f"🔬 Iniciando backtest 90 dias {symbol}...")
    start = time.time()

    # 90 dias de dados: 15M ≈ 8640 candles, 1H ≈ 2160, 4H ≈ 540
    CANDLES_90D_15M = 8640
    CANDLES_90D_1H  = 2160
    CANDLES_90D_4H  = 540

    k15 = await fetch_history(client, symbol, "15",  CANDLES_90D_15M)
    k1h = await fetch_history(client, symbol, "60",  CANDLES_90D_1H)
    k4h = await fetch_history(client, symbol, "240", CANDLES_90D_4H)

    if not k15 or len(k15) < 100:
        return {"error": "Dados históricos insuficientes (min 100 candles 15M)"}

    actual_days = round(len(k15) * 15 / (60 * 24), 1)
    log.info(
        f"📊 Dados carregados: {len(k15)} candles 15M "
        f"({actual_days} dias) | {len(k1h)} x1H | {len(k4h)} x4H"
    )

    # ── Backtest completo (in-sample total) ──────────────────────
    trades  = _run_strategy(k15, k1h, k4h)
    metrics = _calc_metrics(trades, "MTF-4H-1H-15M")

    # ── Walk-Forward Testing (out-of-sample) ─────────────────────
    wf = _walk_forward(k15, k1h, k4h, n_windows=3, train_ratio=0.70)

    elapsed = round(time.time() - start, 1)
    metrics["elapsed_seconds"]  = elapsed
    metrics["symbol"]           = symbol
    metrics["candles_analyzed"] = len(k15)
    metrics["days_analyzed"]    = actual_days
    metrics["ran_at"]           = datetime.now(timezone.utc).isoformat()
    metrics["walk_forward"]     = wf

    # ── Monte Carlo Permutation Test ─────────────────────────────
    trade_returns = [t.get("pnl_pct", 0) for t in trades if "pnl_pct" in t]
    mc = monte_carlo_permutation(trade_returns)
    metrics["monte_carlo"] = mc
    if not mc.get("edge_significant", True):
        log.warning(
            f"⚠️  Monte Carlo {symbol}: p-value={mc.get('p_value','?')} "
            f"→ {mc.get('verdict','?')} — "
            f"considere NÃO operar com capital real"
        )
    else:
        log.info(
            f"✅ Monte Carlo {symbol}: p-value={mc.get('p_value','?')} "
            f"({mc.get('confidence_level','?')}) | "
            f"Sharpe no percentil {mc.get('sharpe_percentile','?')}% das simulações"
        )

    log.info(
        f"✅ Backtest {symbol} | {actual_days:.0f} dias | {elapsed}s | "
        f"{len(trades)} trades | WR={metrics.get('win_rate')}% | "
        f"PF={metrics.get('profit_factor')} | Sharpe={metrics.get('sharpe_ratio')} | "
        f"OOS_WR={wf.get('oos_win_rate','?')}% | "
        f"Overfit={wf.get('overfit_risk','?')}"
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
# Lock compartilhado entre backtest e otimização (BT-3)
_backtest_lock = asyncio.Lock()


async def weekly_backtest_loop(client):
    """
    Roda backtest toda semana automaticamente (domingo 03:00 UTC).
    BT-3: usa _backtest_lock para não rodar simultâneo com otimização.
    """
    while True:
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if now.weekday() == 6 and now.hour == 3 and now.minute < 5:
                if _backtest_lock.locked():
                    log.info("📅 Backtest semanal: lock ativo, aguardando...")
                else:
                    async with _backtest_lock:
                        log.info("📅 Backtest semanal automático (90 dias + walk-forward)...")
                        for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]:
                            try:
                                await run_backtest(client, sym)
                                await asyncio.sleep(30)  # pausa entre símbolos
                            except Exception as e:
                                log.error(f"weekly_backtest {sym}: {e}")
                await asyncio.sleep(3600)  # evita re-execução na mesma hora
        except Exception as e:
            log.error(f"weekly_backtest_loop: {e}")
        await asyncio.sleep(60)
