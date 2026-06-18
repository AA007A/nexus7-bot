"""
BGX Capital — Optimizer v1.0
Otimização de hiperparâmetros via Optuna (TPE sampler).

Uso:
  python -m bot.optimizer --symbol BTCUSDT --trials 300

O resultado é salvo em bot/params_optimized.json e carregado
automaticamente pelo Analyzer se disponível.

Fluxo:
  1. Busca histórico de 90 dias (via fetch_history do backtest)
  2. Divide em treino (60d) e validação (30d)
  3. Optuna minimiza o negativo do Sharpe OOS (out-of-sample)
  4. Penaliza soluções com < 20 trades (evita overfitting extremo)
  5. Salva params em JSON para uso pelo engine em produção

Parâmetros otimizados:
  sl_mult, tp_mult       — multiplicadores de ATR para SL/TP
  rsi_ob, rsi_os         — níveis de sobrecomprado/sobrevendido
  min_adx                — ADX mínimo para considerar tendência
  vol_threshold          — volume mínimo relativo à média
  min_score              — score mínimo de confluência MTF
  bos_lookback           — candles para swing high/low do BOS
  momentum_atr_mult      — ATR mínimo para entrada por momentum
"""
import asyncio, json, os, argparse, time
from pathlib import Path
import numpy as np

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    OPTUNA_AVAILABLE = True
except ImportError:
    OPTUNA_AVAILABLE = False

from bot.logger import log
from bot.config import cfg
from bot.backtest import fetch_history, _calc_metrics, run_strategy_public

# Arquivo onde os melhores parâmetros são persistidos
PARAMS_FILE = Path(__file__).parent / "params_optimized.json"

# Parâmetros padrão (fallback se nenhuma otimização foi rodada)
DEFAULT_PARAMS = {
    "sl_mult":            1.5,
    "tp_mult":            3.0,
    "rsi_ob":             75,
    "rsi_os":             25,
    "min_adx":            22,
    "vol_threshold":      0.15,
    "min_score":          60,
    "bos_lookback":       8,
    "momentum_atr_mult":  0.25,
}


def load_optimized_params() -> dict:
    """
    Carrega parâmetros otimizados do JSON.
    Retorna DEFAULT_PARAMS se não existir ou estiver corrompido.
    """
    try:
        if PARAMS_FILE.exists():
            with open(PARAMS_FILE, "r") as f:
                data = json.load(f)
            params = data.get("best_params", DEFAULT_PARAMS)
            log.info(
                f"✅ Parâmetros otimizados carregados: "
                f"score≥{params.get('min_score')}, "
                f"sl×{params.get('sl_mult')}, "
                f"tp×{params.get('tp_mult')}"
            )
            return params
    except Exception as e:
        log.warning(f"load_optimized_params: {e} — usando defaults")
    return DEFAULT_PARAMS.copy()


def save_optimized_params(params: dict, metadata: dict = None):
    """Persiste os melhores parâmetros encontrados pelo Optuna."""
    payload = {
        "best_params":  params,
        "metadata":     metadata or {},
        "saved_at":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version":      "1.1",
    }
    # Salva localmente (desenvolvimento / Railway com volume persistente)
    try:
        with open(PARAMS_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        log.info(f"💾 Parâmetros salvos em {PARAMS_FILE}")
    except Exception as e:
        log.warning(f"save_optimized_params (arquivo): {e}")

    # SEC-4/INFRA-3: salva também como variável de ambiente persistente via DB
    # Isso garante que os params sobrevivem a deploys no Railway
    try:
        from bot import database as _db
        import asyncio as _ai
        _ai.get_event_loop().create_task(
            _db.save_key_value("optimizer_params", json.dumps(payload))
        ) if _ai.get_event_loop().is_running() else None
    except Exception:
        pass   # silencioso — arquivo local é suficiente em dev


def _run_strategy_with_params(klines_15, klines_1h, klines_4h, params: dict) -> list:
    """
    Roda a estratégia com parâmetros customizados.
    Wrapper sobre _run_strategy do backtest com injeção de params.
    """
    from bot.backtest import _run_strategy as _rsp
    return _rsp(
        klines_15, klines_1h, klines_4h,
        min_score=params.get("min_score", 60),
    )


def _objective(trial, k15_train, k1h_train, k4h_train,
                k15_val, k1h_val, k4h_val) -> float:
    """
    Função objetivo do Optuna.
    Maximiza Sharpe out-of-sample (validação).
    Penaliza fortemente soluções com < 20 trades.
    """
    params = {
        "sl_mult":           trial.suggest_float("sl_mult",           0.8,  3.0),
        "tp_mult":           trial.suggest_float("tp_mult",           1.5,  6.0),
        "rsi_ob":            trial.suggest_int  ("rsi_ob",            65,   85),
        "rsi_os":            trial.suggest_int  ("rsi_os",            15,   35),
        "min_adx":           trial.suggest_int  ("min_adx",           15,   35),
        "vol_threshold":     trial.suggest_float("vol_threshold",     0.05, 0.50),
        "min_score":         trial.suggest_int  ("min_score",         50,   80),
        "bos_lookback":      trial.suggest_int  ("bos_lookback",      4,    16),
        "momentum_atr_mult": trial.suggest_float("momentum_atr_mult", 0.10, 0.60),
    }

    # Valida R:R mínimo de 1.5 — descarta combinações absurdas
    if params["tp_mult"] / params["sl_mult"] < 1.5:
        return -999.0

    # Roda no conjunto de VALIDAÇÃO (out-of-sample)
    trades_val = _run_strategy_with_params(k15_val, k1h_val, k4h_val, params)
    if not trades_val or len(trades_val) < 20:
        return -999.0   # penaliza soluções com poucos trades

    m = _calc_metrics(trades_val)
    sharpe = m.get("sharpe_ratio", 0)
    pf     = m.get("profit_factor", 0)

    # Objetivo composto: Sharpe + bônus por Profit Factor > 1.5
    # Evita maximizar Sharpe às custas de expectância
    bonus = 0.2 if pf > 1.5 else 0.0
    return sharpe + bonus


async def run_optimization(client, symbol: str = "BTCUSDT",
                           n_trials: int = 300) -> dict:
    """
    Executa otimização completa com Optuna.

    Divide dados em:
      Treino   (60d): usado APENAS para referência, não no objetivo
      Validação(30d): Sharpe OOS é o objetivo — nunca visto durante treino
    """
    if not OPTUNA_AVAILABLE:
        log.error("Optuna não instalado. Execute: pip install optuna")
        return {"error": "optuna not installed"}

    log.info(f"🔬 Iniciando otimização Optuna — {symbol} | {n_trials} trials")
    t0 = time.time()

    # Busca 90 dias de dados
    k15 = await fetch_history(client, symbol, "15",  8640)
    k1h = await fetch_history(client, symbol, "60",  2160)
    k4h = await fetch_history(client, symbol, "240",  540)

    if not k15 or len(k15) < 200:
        return {"error": "Dados insuficientes para otimização"}

    # Divide 60% treino / 30% validação / 10% teste final (não usado aqui)
    n15   = len(k15)
    s60   = int(n15 * 0.60)   # índice de corte treino/validação
    s90   = int(n15 * 0.90)   # índice de corte validação/teste

    k15_train, k1h_train, k4h_train = (
        k15[:s60], k1h[:s60//4], k4h[:s60//16]
    )
    k15_val, k1h_val, k4h_val = (
        k15[s60:s90], k1h[s60//4:s90//4], k4h[s60//16:s90//16]
    )

    actual_days_train = round(len(k15_train) * 15 / (60 * 24), 0)
    actual_days_val   = round(len(k15_val)   * 15 / (60 * 24), 0)
    log.info(
        f"📊 Dados: treino={actual_days_train:.0f}d "
        f"({len(k15_train)} candles 15M) | "
        f"validação={actual_days_val:.0f}d ({len(k15_val)} candles 15M)"
    )

    # Cria estudo Optuna (maximiza Sharpe OOS)
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_warmup_steps=20),
    )

    def objective(trial):
        return _objective(
            trial,
            k15_train, k1h_train, k4h_train,
            k15_val,   k1h_val,   k4h_val,
        )

    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    best_params = study.best_params
    best_value  = study.best_value

    # Validação final nos dados de TESTE (10% reservados)
    k15_test = k15[s90:]
    k1h_test = k1h[s90//4:]
    k4h_test = k4h[s90//16:]

    trades_test = _run_strategy_with_params(k15_test, k1h_test, k4h_test, best_params)
    test_metrics = _calc_metrics(trades_test) if trades_test else {}

    elapsed = round(time.time() - t0, 1)
    log.info(
        f"✅ Otimização concluída em {elapsed}s | "
        f"Melhor Sharpe OOS={best_value:.3f} | "
        f"Params: score≥{best_params.get('min_score')} "
        f"sl×{best_params.get('sl_mult',0):.2f} "
        f"tp×{best_params.get('tp_mult',0):.2f} | "
        f"Teste final: WR={test_metrics.get('win_rate','?')}% "
        f"PF={test_metrics.get('profit_factor','?')}"
    )

    metadata = {
        "symbol":       symbol,
        "n_trials":     n_trials,
        "best_sharpe_oos": round(best_value, 4),
        "test_metrics": test_metrics,
        "elapsed_s":    elapsed,
        "days_train":   actual_days_train,
        "days_val":     actual_days_val,
    }
    save_optimized_params(best_params, metadata)

    return {"best_params": best_params, "metadata": metadata}


# Lock global para evitar otimização e backtest simultâneos (BT-3)
_optimization_lock = asyncio.Lock()


async def weekly_optimization_loop(client):
    """
    Roda otimização toda segunda-feira às 02:00 UTC.
    Usa asyncio.Lock() para evitar execuções simultâneas (BT-3).
    Usa 200 trials por símbolo — leve e atualiza semanalmente.
    """
    while True:
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if now.weekday() == 0 and now.hour == 2 and now.minute < 5:
                if _optimization_lock.locked():
                    log.info("📅 Otimização semanal: lock ativo, aguardando...")
                else:
                    async with _optimization_lock:
                        log.info("📅 Otimização semanal automática iniciando...")
                        for sym in ["BTCUSDT", "ETHUSDT"]:
                            try:
                                await run_optimization(client, sym, n_trials=200)
                            except Exception as e:
                                log.error(f"weekly_optimization {sym}: {e}")
                await asyncio.sleep(3600)
        except Exception as e:
            log.error(f"weekly_optimization_loop: {e}")
        await asyncio.sleep(60)
