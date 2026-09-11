"""BGX Capital — Backtesting Engine.

Quant-audit hardening:
- KuCoin historical pagination uses explicit millisecond time windows.
- Candles are uniquely keyed by timestamp, never by price.
- Historical integrity checks detect duplicates, non-monotonic data and gaps.
- Trade hour/day are derived from the real candle timestamp.
- Same-bar SL/TP ambiguity is resolved conservatively (stop first).
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Dict, List

import numpy as np

from bot.config import cfg
from bot.logger import log


def _interval_minutes(interval: str) -> int:
    s = str(interval)
    if s == "D":
        return 1440
    if s == "W":
        return 10080
    return int(s)


def _normalize_kline(raw) -> dict | None:
    """Normalize one KuCoin Futures kline to the project's candle schema."""
    try:
        ts = int(float(raw[0]))
        if ts < 1e11:
            ts *= 1000
        candle = {
            "ts": ts,
            "o": float(raw[1]),
            "h": float(raw[2]),
            "l": float(raw[3]),
            "c": float(raw[4]),
            "v": float(raw[5]),
        }
        if candle["h"] < candle["l"] or not (candle["l"] <= candle["c"] <= candle["h"]):
            return None
        if min(candle["o"], candle["h"], candle["l"], candle["c"]) <= 0:
            return None
        return candle
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        return None


def _historical_integrity(candles: list, interval: str) -> dict:
    """Return deterministic integrity telemetry for a chronological series."""
    if not candles:
        return {"ok": False, "duplicates": 0, "non_monotonic": 0, "gaps": 0}
    expected = _interval_minutes(interval) * 60 * 1000
    timestamps = [int(c["ts"]) for c in candles]
    duplicates = len(timestamps) - len(set(timestamps))
    non_monotonic = sum(1 for a, b in zip(timestamps, timestamps[1:]) if b <= a)
    # Allow up to 10% timestamp jitter; exchange outages are surfaced as gaps.
    gaps = sum(1 for a, b in zip(timestamps, timestamps[1:]) if b - a > expected * 1.10)
    return {
        "ok": duplicates == 0 and non_monotonic == 0,
        "duplicates": duplicates,
        "non_monotonic": non_monotonic,
        "gaps": gaps,
    }


async def _kucoin_page(client, symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Read a bounded KuCoin Futures kline page without mutating live caches."""
    from bot.kucoin import to_kucoin

    data = await client._get(
        "/api/v1/kline/query",
        {
            "symbol": to_kucoin(symbol),
            "granularity": str(_interval_minutes(interval)),
            "from": str(int(start_ms)),
            "to": str(int(end_ms)),
        },
    )
    raw = data if isinstance(data, list) else []
    out = []
    for item in raw:
        candle = _normalize_kline(item)
        if candle is not None:
            out.append(candle)
    out.sort(key=lambda c: c["ts"])
    return out


async def fetch_history(client, symbol: str, interval: str, limit: int = 1000) -> list:
    """Fetch exactly bounded historical OHLCV using timestamp pagination.

    H01 fix: the old implementation repeatedly requested the latest window and
    de-duplicated by opening *price*. This implementation pages backwards by
    timestamp and de-duplicates only by candle timestamp.
    """
    if limit <= 0:
        return []

    try:
        # Real KuCoin client exposes _get. Keep a conservative compatibility
        # fallback for mocks/alternate clients used by tests.
        if not hasattr(client, "_get"):
            rows = await client.get_klines(symbol, interval, limit)
            unique = {int(c["ts"]): c for c in rows if c and c.get("ts")}
            result = sorted(unique.values(), key=lambda c: c["ts"])[-limit:]
            integ = _historical_integrity(result, interval)
            if not integ["ok"]:
                raise RuntimeError(f"historical integrity failed: {integ}")
            return result

        interval_ms = _interval_minutes(interval) * 60 * 1000
        # KuCoin Futures safely supports bounded kline windows; 500 keeps each
        # request small and deterministic across granularities.
        page_size = 500
        cursor_end = int(time.time() * 1000)
        by_ts: dict[int, dict] = {}
        previous_oldest: int | None = None
        max_pages = max(2, (limit + page_size - 1) // page_size + 3)

        for _ in range(max_pages):
            if len(by_ts) >= limit:
                break
            remaining = limit - len(by_ts)
            requested = min(page_size, remaining)
            # Add one interval of overlap; timestamp de-dupe makes overlap safe
            # and protects against exchange boundary semantics.
            span = interval_ms * (requested + 1)
            cursor_start = max(0, cursor_end - span)
            page = await _kucoin_page(client, symbol, interval, cursor_start, cursor_end)
            if not page:
                break

            for candle in page:
                by_ts[int(candle["ts"])] = candle

            oldest = min(int(c["ts"]) for c in page)
            if previous_oldest is not None and oldest >= previous_oldest:
                raise RuntimeError(
                    f"historical pagination made no progress: oldest={oldest} previous={previous_oldest}"
                )
            previous_oldest = oldest
            cursor_end = oldest - 1
            await asyncio.sleep(0.05)

        result = sorted(by_ts.values(), key=lambda c: c["ts"])[-limit:]
        integrity = _historical_integrity(result, interval)
        if not integrity["ok"]:
            raise RuntimeError(f"historical integrity failed: {integrity}")
        if integrity["gaps"]:
            log.warning(
                f"[BACKTEST_DATA] {symbol} {interval}m: {integrity['gaps']} historical gaps detected"
            )
        if len(result) < min(limit, 100):
            log.warning(
                f"[BACKTEST_DATA] {symbol} {interval}m: requested={limit} received={len(result)}"
            )
        log.info(
            f"fetch_history {symbol} {interval}: {len(result)} candles | "
            f"unique_ts={len({c['ts'] for c in result})} gaps={integrity['gaps']}"
        )
        return result
    except Exception as exc:
        log.error(f"backtest fetch {symbol} {interval}: {type(exc).__name__}: {exc}")
        return []


def _run_strategy(
    klines_15: list,
    klines_1h: list,
    klines_4h: list,
    min_score: int = 75,
    min_rr: float = 2.0,
    sl_mult: float | None = None,
    tp_mult: float | None = None,
    symbol: str = "",
) -> List[dict]:
    """Replay the production MTF strategy on chronological historical data."""
    from bot.strategy import Analyzer

    _orig_sl = getattr(cfg, "SL_ATR_MULT", 1.5)
    _orig_tp = getattr(cfg, "TP_ATR_MULT", 3.0)
    if sl_mult is not None:
        cfg.SL_ATR_MULT = float(sl_mult)
    if tp_mult is not None:
        cfg.TP_ATR_MULT = float(tp_mult)

    analyzer = Analyzer()
    trades: list[dict] = []
    analysis_errors = 0
    WINDOW = 60

    try:
        for i in range(WINDOW, len(klines_15) - 1):
            k15 = klines_15[max(0, i - WINDOW):i]
            k1h = klines_1h[max(0, i // 4 - 20):i // 4]
            k4h = klines_4h[max(0, i // 16 - 15):i // 16]
            if len(k15) < 30 or len(k1h) < 10 or len(k4h) < 5:
                continue

            try:
                sig = analyzer.analyze_mtf(
                    symbol or "BT",
                    k15,
                    k1h,
                    k4h,
                    min_score=int(min_score),
                    fee_mult=getattr(cfg, "FEE_MULTIPLIER", 2.0),
                    vol_mult=getattr(cfg, "MIN_VOLUME_MULT", 1.2),
                )
            except Exception as exc:
                analysis_errors += 1
                if analysis_errors % 100 == 1:
                    log.debug(f"backtest analysis error candle={i}: {exc}")
                continue

            if not sig or sig.rr < float(min_rr):
                continue

            entry, sl, tp = float(sig.entry), float(sig.sl), float(sig.tp)
            tp1 = float(getattr(sig, "tp1", tp) or tp)
            tp2 = float(getattr(sig, "tp2", tp) or tp)
            has_partial = tp1 != tp2 and tp1 != 0
            result = None
            hold = 0
            tp1_hit = False
            pnl_pct = 0.0
            ambiguous_bars = 0

            taker = float(os.environ.get("TAKER_FEE", "0.0006"))
            fee_pct = taker * 2
            slip_base = float(os.environ.get("BACKTEST_SLIPPAGE", "0.0005"))
            majors = ("BTC", "ETH", "SOL")
            sym_up = str(symbol).upper()
            slip = slip_base if any(m in sym_up for m in majors) else slip_base * 2
            cost_pct = fee_pct + slip * 2
            funding_8h = float(os.environ.get("BACKTEST_FUNDING", "0.0001"))

            for j in range(i + 1, min(i + 41, len(klines_15))):
                future = klines_15[j]
                hold += 1
                high, low = float(future["h"]), float(future["l"])

                # H06 hardening: if SL and target are both inside one OHLC bar,
                # tick ordering is unknowable. Resolve to the stop side first.
                if sig.direction == "LONG":
                    target_now = tp2 if tp1_hit else (tp1 if has_partial else tp)
                    if low <= sl and high >= target_now:
                        ambiguous_bars += 1
                    if low <= sl:
                        if tp1_hit:
                            pnl_pct = abs(tp1 - entry) / entry * 0.5
                            pnl_pct -= cost_pct + funding_8h * max(1, hold * 15 / 480)
                            result = "PARTIAL_WIN"
                        else:
                            pnl_pct = -(abs(sl - entry) / entry)
                            pnl_pct -= cost_pct + funding_8h * max(1, hold * 15 / 480)
                            result = "LOSS"
                        break
                    if has_partial and not tp1_hit and high >= tp1:
                        tp1_hit = True
                        sl = entry
                    if tp1_hit and high >= tp2:
                        pnl_pct = (abs(tp1 - entry) + abs(tp2 - entry)) / entry * 0.5
                        pnl_pct -= cost_pct + funding_8h * max(1, hold * 15 / 480)
                        result = "WIN"
                        break
                    if not has_partial and high >= tp:
                        pnl_pct = abs(tp - entry) / entry
                        pnl_pct -= cost_pct + funding_8h * max(1, hold * 15 / 480)
                        result = "WIN"
                        break
                else:
                    target_now = tp2 if tp1_hit else (tp1 if has_partial else tp)
                    if high >= sl and low <= target_now:
                        ambiguous_bars += 1
                    if high >= sl:
                        if tp1_hit:
                            pnl_pct = abs(tp1 - entry) / entry * 0.5
                            pnl_pct -= cost_pct + funding_8h * max(1, hold * 15 / 480)
                            result = "PARTIAL_WIN"
                        else:
                            pnl_pct = -(abs(sl - entry) / entry)
                            pnl_pct -= cost_pct + funding_8h * max(1, hold * 15 / 480)
                            result = "LOSS"
                        break
                    if has_partial and not tp1_hit and low <= tp1:
                        tp1_hit = True
                        sl = entry
                    if tp1_hit and low <= tp2:
                        pnl_pct = (abs(tp1 - entry) + abs(tp2 - entry)) / entry * 0.5
                        pnl_pct -= cost_pct + funding_8h * max(1, hold * 15 / 480)
                        result = "WIN"
                        break
                    if not has_partial and low <= tp:
                        pnl_pct = abs(tp - entry) / entry
                        pnl_pct -= cost_pct + funding_8h * max(1, hold * 15 / 480)
                        result = "WIN"
                        break

            if result is None:
                result = "TIMEOUT"
                last = float(klines_15[min(i + 40, len(klines_15) - 1)]["c"])
                base = (last - entry) / entry * (1 if sig.direction == "LONG" else -1)
                pnl_pct = base * (0.5 if tp1_hit else 1.0)
                pnl_pct -= cost_pct + funding_8h * max(1, hold * 15 / 480)

            # H05 fix: derive calendar attributes from the real candle timestamp.
            ts_ms = int(klines_15[i].get("ts", 0) or 0)
            if ts_ms < 1e11:
                ts_ms *= 1000
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            trades.append(
                {
                    "result": result,
                    "pnl_pct": pnl_pct,
                    "hold": hold,
                    "direction": sig.direction,
                    "score": sig.score,
                    "hour_utc": dt.hour,
                    "day_of_week": dt.weekday(),
                    "opened_at": dt.isoformat(),
                    "ts": ts_ms,
                    "rr": sig.rr,
                    "regime": getattr(sig, "regime", "UNKNOWN"),
                    "entry_type": getattr(sig, "entry_type", "UNKNOWN"),
                    "intrabar_ambiguous": ambiguous_bars,
                }
            )
    finally:
        cfg.SL_ATR_MULT = _orig_sl
        cfg.TP_ATR_MULT = _orig_tp

    return trades


def _calc_metrics(trades: List[dict], strategy: str = "MTF") -> dict:
    if not trades:
        return {}
    pnls = np.array([t["pnl_pct"] for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    total = len(pnls)
    win_rate = len(wins) / total * 100 if total else 0.0
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(abs(losses.sum())) if len(losses) else 1e-9
    pf = gross_profit / gross_loss
    std = float(pnls.std())
    sharpe = float(pnls.mean() / std) if std > 0 and total > 1 else 0.0
    neg_std = float(losses.std()) if len(losses) > 1 else 0.0
    sortino = float(pnls.mean() / neg_std) if neg_std > 0 else 0.0
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    max_dd = float((peak - cum).max()) if len(cum) else 0.0

    hour_pnl: Dict[int, list] = {}
    day_pnl: Dict[int, list] = {}
    for t in trades:
        hour_pnl.setdefault(int(t["hour_utc"]), []).append(t["pnl_pct"])
        day_pnl.setdefault(int(t["day_of_week"]), []).append(t["pnl_pct"])
    hour_avg = {h: float(np.mean(v)) for h, v in hour_pnl.items()}
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    day_avg = {days[d]: float(np.mean(v)) for d, v in day_pnl.items() if 0 <= d <= 6}
    ambiguous = sum(int(t.get("intrabar_ambiguous", 0)) for t in trades)

    return {
        "strategy": strategy,
        "total_trades": total,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(pf, 2),
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "expectancy_pct": round(float(pnls.mean()) * 100, 4),
        "avg_hold_candles": round(float(np.mean([t["hold"] for t in trades])), 1),
        "best_hour_utc": max(hour_avg, key=hour_avg.get) if hour_avg else None,
        "worst_hour_utc": min(hour_avg, key=hour_avg.get) if hour_avg else None,
        "best_day": max(day_avg, key=day_avg.get) if day_avg else None,
        "worst_day": min(day_avg, key=day_avg.get) if day_avg else None,
        "gross_profit_pct": round(gross_profit * 100, 2),
        "gross_loss_pct": round(gross_loss * 100, 2),
        "intrabar_ambiguous_bars": ambiguous,
    }


run_strategy_public = _run_strategy


def monte_carlo_permutation(returns: list, n_simulations: int = 5000, random_seed: int = 42) -> dict:
    """Permutation diagnostic retained for compatibility.

    Note: permuting return order does not change mean/std Sharpe. We retain the
    historical interface but label it diagnostic rather than using it as proof
    of alpha; strategy validity relies primarily on untouched OOS/walk-forward.
    """
    if not returns or len(returns) < 10:
        return {"error": "Mínimo 10 trades para Monte Carlo", "edge_significant": False}
    arr = np.array(returns, dtype=float)
    rng = np.random.default_rng(random_seed)
    real_sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0
    # Bootstrap signed returns to obtain a non-degenerate empirical distribution.
    sims = np.zeros(n_simulations)
    for i in range(n_simulations):
        sample = rng.choice(arr, size=len(arr), replace=True)
        s = sample.std()
        sims[i] = float(sample.mean() / s) if s > 0 else 0.0
    p_value = float((sims <= 0.0).mean())
    return {
        "real_sharpe": round(real_sharpe, 4),
        "p_value": round(p_value, 4),
        "confidence_level": "HIGH" if p_value < 0.05 else "MEDIUM" if p_value < 0.10 else "NONE",
        "verdict": "Edge bootstrap positivo" if p_value < 0.10 else "Sem evidência bootstrap suficiente",
        "edge_significant": p_value < 0.05,
        "sharpe_percentile": round(float((sims < real_sharpe).mean() * 100), 1),
        "random_sharpe_mean": round(float(sims.mean()), 4),
        "random_sharpe_std": round(float(sims.std()), 4),
        "n_simulations": n_simulations,
        "n_trades": len(returns),
    }


def _walk_forward(
    klines_15: list,
    klines_1h: list,
    klines_4h: list,
    n_windows: int = 5,
    train_ratio: float = 0.70,
    symbol: str = "",
    rolling: bool = True,
) -> dict:
    if len(klines_15) < 200:
        return {"error": "Dados insuficientes para walk-forward (min 200 candles 15M)"}
    n = len(klines_15)
    results = []
    if rolling and n_windows > 1:
        window_size = int(n / ((n_windows + 1) / 2))
        step = max(1, window_size // 2)
    else:
        window_size = n // n_windows
        step = window_size

    for w in range(n_windows):
        start_idx = w * step
        end_idx = min(start_idx + window_size, n)
        if end_idx - start_idx < 120 or start_idx >= n:
            continue
        w15 = klines_15[start_idx:end_idx]
        w1 = klines_1h[start_idx // 4:end_idx // 4]
        w4 = klines_4h[start_idx // 16:end_idx // 16]
        split = int(len(w15) * train_ratio)
        train_15, test_15 = w15[:split], w15[split:]
        train_1, test_1 = w1[:split // 4], w1[split // 4:]
        train_4, test_4 = w4[:split // 16], w4[split // 16:]
        train_trades = _run_strategy(train_15, train_1, train_4, symbol=symbol) if len(train_15) >= 60 else []
        test_trades = _run_strategy(test_15, test_1, test_4, symbol=symbol) if len(test_15) >= 30 else []
        train_m = _calc_metrics(train_trades, "train") if train_trades else {}
        test_m = _calc_metrics(test_trades, "test") if test_trades else {}
        results.append(
            {
                "window": w + 1,
                "candles_train": len(train_15),
                "candles_test": len(test_15),
                "train": {
                    "win_rate": train_m.get("win_rate", 0),
                    "profit_factor": train_m.get("profit_factor", 0),
                    "sharpe": train_m.get("sharpe_ratio", 0),
                    "total_trades": train_m.get("total_trades", 0),
                },
                "test": {
                    "win_rate": test_m.get("win_rate", 0),
                    "profit_factor": test_m.get("profit_factor", 0),
                    "sharpe": test_m.get("sharpe_ratio", 0),
                    "total_trades": test_m.get("total_trades", 0),
                },
            }
        )

    if not results:
        return {"error": "Nenhuma janela com dados suficientes"}
    tests = [r["test"] for r in results if r["test"]["total_trades"] > 0]
    trains = [r["train"] for r in results if r["train"]["total_trades"] > 0]
    oos_wr = float(np.mean([r["win_rate"] for r in tests] or [0]))
    oos_pf = float(np.mean([r["profit_factor"] for r in tests] or [0]))
    is_wr = float(np.mean([r["win_rate"] for r in trains] or [0]))
    is_pf = float(np.mean([r["profit_factor"] for r in trains] or [0]))
    wr_deg = round((is_wr - oos_wr) / max(is_wr, 1) * 100, 1)
    pf_deg = round((is_pf - oos_pf) / max(is_pf, 0.01) * 100, 1)
    avg_deg = (wr_deg + pf_deg) / 2
    return {
        "windows": results,
        "oos_win_rate": round(oos_wr, 1),
        "oos_pf": round(oos_pf, 2),
        "is_win_rate": round(is_wr, 1),
        "is_pf": round(is_pf, 2),
        "wr_degradation_pct": wr_deg,
        "pf_degradation_pct": pf_deg,
        "overfit_risk": "HIGH" if avg_deg > 40 else "MEDIUM" if avg_deg > 20 else "LOW",
        "n_windows": len(results),
    }


async def run_backtest(client, symbol: str = "BTCUSDT") -> dict:
    log.info(f"🔬 Iniciando backtest histórico {symbol}...")
    start = time.time()
    k15 = await fetch_history(client, symbol, "15", 70080)
    k1h = await fetch_history(client, symbol, "60", 17520)
    k4h = await fetch_history(client, symbol, "240", 4380)
    if len(k15) < 100:
        return {"error": "Dados históricos insuficientes (min 100 candles 15M)"}

    trades = _run_strategy(k15, k1h, k4h, symbol=symbol)
    metrics = _calc_metrics(trades, "MTF-4H-1H-15M")
    wf = _walk_forward(k15, k1h, k4h, n_windows=6, train_ratio=0.70, symbol=symbol, rolling=True)
    mc = monte_carlo_permutation([t["pnl_pct"] for t in trades])
    metrics.update(
        {
            "elapsed_seconds": round(time.time() - start, 1),
            "symbol": symbol,
            "candles_analyzed": len(k15),
            "days_analyzed": round(len(k15) * 15 / (60 * 24), 1),
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "walk_forward": wf,
            "monte_carlo": mc,
        }
    )
    metrics["strategy_validity"] = check_strategy_validity(metrics, wf, mc)

    try:
        from bot import database as dbase
        await dbase._exec(
            """INSERT INTO performance
               (periodo,strategy,win_rate,profit_factor,sharpe_ratio,sortino_ratio,
                max_drawdown,expectancy_por_trade,total_trades,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).date().isoformat(),
                metrics.get("strategy", "MTF"),
                metrics.get("win_rate", 0),
                metrics.get("profit_factor", 0),
                metrics.get("sharpe_ratio", 0),
                metrics.get("sortino_ratio", 0),
                metrics.get("max_drawdown_pct", 0) / 100,
                metrics.get("expectancy_pct", 0) / 100,
                metrics.get("total_trades", 0),
                metrics["ran_at"],
            ),
        )
    except Exception as exc:
        log.error(f"backtest persist: {exc}")
    return metrics


_backtest_lock = asyncio.Lock()


async def weekly_backtest_loop(client):
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() == 6 and now.hour == 3 and now.minute < 5:
                if not _backtest_lock.locked():
                    async with _backtest_lock:
                        for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
                            try:
                                await run_backtest(client, sym)
                                await asyncio.sleep(30)
                            except Exception as exc:
                                log.error(f"weekly_backtest {sym}: {exc}")
                await asyncio.sleep(3600)
        except Exception as exc:
            log.error(f"weekly_backtest_loop: {exc}")
        await asyncio.sleep(60)


def check_strategy_validity(metrics: dict, wf: dict, mc: dict) -> dict:
    warnings, critical = [], []
    pf_oos = wf.get("oos_pf", 0)
    wr_oos = wf.get("oos_win_rate", 0)
    test_windows = [w.get("test", {}) for w in wf.get("windows", []) if w.get("test", {}).get("total_trades", 0)]
    sharpe_oos = float(np.mean([w.get("sharpe", 0) for w in test_windows] or [0]))
    overfit = wf.get("overfit_risk", "UNKNOWN")
    p_value = mc.get("p_value", 1.0)
    total_trades = metrics.get("total_trades", 0)
    if total_trades < 100:
        warnings.append(f"Amostra pequena: {total_trades} trades")
    if total_trades < 50:
        critical.append(f"CRÍTICO: {total_trades} trades insuficientes")
    if pf_oos < 1.0:
        critical.append(f"CRÍTICO: PF OOS={pf_oos:.2f} < 1.0")
    elif pf_oos < 1.10:
        warnings.append(f"PF OOS={pf_oos:.2f} marginal")
    if wr_oos < 35:
        critical.append(f"CRÍTICO: WR OOS={wr_oos:.1f}% < 35%")
    if p_value >= 0.10:
        critical.append(f"CRÍTICO: bootstrap p-value={p_value:.4f} >= 0.10")
    elif p_value >= 0.05:
        warnings.append(f"bootstrap p-value={p_value:.4f} — edge fraco")
    if sharpe_oos < 0:
        critical.append(f"CRÍTICO: Sharpe OOS={sharpe_oos:.2f} < 0")
    elif sharpe_oos < 0.5:
        warnings.append(f"Sharpe OOS={sharpe_oos:.2f} baixo")
    if overfit == "HIGH":
        critical.append("CRÍTICO: Overfit risk=HIGH")
    elif overfit == "MEDIUM":
        warnings.append("Overfit risk=MEDIUM")

    if critical:
        valid, verdict, action = False, "INVALIDADA", "SUSPENDER escala live; revisar e re-testar."
    elif len(warnings) >= 3:
        valid, verdict, action = False, "QUESTIONÁVEL", "Manter apenas paper/shadow/pilot mínimo."
    elif warnings:
        valid, verdict, action = True, "ACEITÁVEL_COM_RESSALVAS", "Manter piloto mínimo e coletar mais OOS."
    else:
        valid, verdict, action = True, "VÁLIDA", "Elegível para revisão de release, não garantia de lucro."
    return {
        "valid": valid,
        "verdict": verdict,
        "action": action,
        "warnings": warnings,
        "critical": critical,
        "pf_oos": pf_oos,
        "wr_oos": wr_oos,
        "sharpe_oos": round(sharpe_oos, 3),
        "p_value": p_value,
        "overfit": overfit,
    }
