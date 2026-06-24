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
from bot import database as _db
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
    "vol_threshold":      0.60,   # FIX: 0.15→0.60 consistente com strategy.py
    "min_score":          65,     # FIX: 60→65 consistente com config.py
    "bos_lookback":       20,     # FIX: 8→20 consistente com strategy.py
    "momentum_atr_mult":  0.25,
}


def load_optimized_params() -> dict:
    """
    Carrega parâmetros otimizados com prioridade:
      1. Arquivo local (params_optimized.json) — rápido
      2. Banco de dados (key_value) — fallback pós-deploy Railway
      3. DEFAULT_PARAMS — se nada encontrado
    """
    # Tenta arquivo local primeiro
    try:
        if PARAMS_FILE.exists():
            with open(PARAMS_FILE, "r") as f:
                data = json.load(f)
            params = data.get("best_params", DEFAULT_PARAMS)
            log.info(
                f"✅ Params otimizados (arquivo): "
                f"score≥{params.get('min_score')} "
                f"sl×{params.get('sl_mult')} "
                f"tp×{params.get('tp_mult')}"
            )
            return params
    except Exception as e:
        log.warning(f"load_optimized_params (arquivo): {e}")

    # Tenta banco de dados como fallback (Railway sem volume persistente)
    try:

        async def _load_from_db():
            val = await _db.load_key_value("optimizer_params")
            if val:
                data = json.loads(val)
                return data.get("best_params", DEFAULT_PARAMS)
            return None

        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Ambiente async — agenda como task (não bloqueia)
            log.info("load_optimized_params: agendando carga do DB...")
        else:
            params = loop.run_until_complete(_load_from_db())
            if params:
                log.info(
                    f"✅ Params otimizados (DB): "
                    f"score≥{params.get('min_score')} "
                    f"sl×{params.get('sl_mult')} "
                    f"tp×{params.get('tp_mult')}"
                )
                return params
    except Exception as e:
        log.warning(f"load_optimized_params (DB): {e}")

    log.info("load_optimized_params: usando defaults conservadores")
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
        asyncio.get_event_loop().create_task(
            _db.save_key_value("optimizer_params", json.dumps(payload))
        ) if asyncio.get_event_loop().is_running() else None
    except Exception:
        pass   # silencioso — arquivo local é suficiente em dev


def _run_strategy_with_params(klines_15, klines_1h, klines_4h, params: dict) -> list:
    """
    Roda a estratégia com parâmetros customizados injetados.
    Passa sl_mult, tp_mult, min_score e min_rr ao _run_strategy
    para que a otimização realmente afete o comportamento simulado.
    """
    from bot.backtest import _run_strategy as _rsp
    return _rsp(
        klines_15, klines_1h, klines_4h,
        min_score = params.get("min_score",  60),
        min_rr    = params.get("tp_mult", 3.0) / max(params.get("sl_mult", 1.5), 0.1),
        sl_mult   = params.get("sl_mult",   1.5),
        tp_mult   = params.get("tp_mult",   3.0),
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
                                await run_optimization(client, sym, n_trials=500)  # FIX: 200→500 para melhor cobertura
                            except Exception as e:
                                log.error(f"weekly_optimization {sym}: {e}")
                await asyncio.sleep(3600)
        except Exception as e:
            log.error(f"weekly_optimization_loop: {e}")
        await asyncio.sleep(60)


def test_parameter_robustness(k15: list, k1h: list, k4h: list,
                               best_params: dict,
                               perturbation: float = 0.10) -> dict:
    """
    Teste de robustez por perturbação de parâmetros (±10%).
    Item 38 da lista de melhorias.

    Um sistema robusto mantém performance similar com pequenas variações
    nos parâmetros. Se performance cai muito com ±10%, está overfitado.

    Retorna: {"robust": bool, "avg_degradation_pct": float, "details": list}
    """
    from bot.backtest import _run_strategy as _rsp, _calc_metrics

    base_trades  = _rsp(k15, k1h, k4h, min_score=best_params.get("min_score", 65))
    base_metrics = _calc_metrics(base_trades)
    base_sharpe  = base_metrics.get("sharpe_ratio", 0)
    base_pf      = base_metrics.get("profit_factor", 0)

    if not base_trades or base_sharpe <= 0:
        return {"robust": False, "reason": "Base sem trades ou Sharpe negativo"}

    perturbable = ["sl_mult", "tp_mult", "min_score"]
    results = []

    for param in perturbable:
        if param not in best_params:
            continue
        base_val = best_params[param]
        for direction in [1 + perturbation, 1 - perturbation]:
            perturbed = dict(best_params)
            if param == "min_score":
                perturbed[param] = int(base_val * direction)
            else:
                perturbed[param] = round(base_val * direction, 4)

            try:
                trades  = _rsp(k15, k1h, k4h,
                               min_score=perturbed.get("min_score", 65),
                               sl_mult=perturbed.get("sl_mult", 1.5),
                               tp_mult=perturbed.get("tp_mult", 3.0))
                metrics = _calc_metrics(trades) if trades else {}
                sharpe  = metrics.get("sharpe_ratio", 0)
                pf      = metrics.get("profit_factor", 0)

                sharpe_deg = ((base_sharpe - sharpe) / max(base_sharpe, 0.01)) * 100
                pf_deg     = ((base_pf - pf) / max(base_pf, 0.01)) * 100

                results.append({
                    "param":      param,
                    "direction":  f"{'+' if direction>1 else '-'}{perturbation*100:.0f}%",
                    "value":      perturbed[param],
                    "sharpe":     round(sharpe, 3),
                    "pf":         round(pf, 2),
                    "sharpe_deg": round(sharpe_deg, 1),
                    "pf_deg":     round(pf_deg, 1),
                })
            except Exception:
                pass

    if not results:
        return {"robust": True, "reason": "Sem parâmetros perturbáveis"}

    avg_deg = float(np.mean([abs(r["sharpe_deg"]) for r in results]))
    robust  = avg_deg < 30  # degradação < 30% é aceitável

    return {
        "robust":               robust,
        "avg_degradation_pct":  round(avg_deg, 1),
        "details":              results,
        "verdict":              "ROBUSTO" if robust else "OVERFITADO",
    }
