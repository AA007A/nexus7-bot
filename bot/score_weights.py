"""
BGX Capital — Score Weight Calibration v1.0
Auditoria #7: os pesos do Entry Score (+10, +5, +3 etc.) eram valores fixos
definidos manualmente em bot/score.py, sem validação estatística — o clássico
"curve fitting por intuição".

Este módulo permite DERIVAR os pesos a partir dos trades históricos reais,
usando regressão logística: quais componentes do score de fato previram
trades vencedores?

Uso:
    from bot.score_weights import calibrate_from_history, load_calibrated_weights

    # Offline (após acumular >= 100 trades):
    result = await calibrate_from_history(min_trades=100)

    # No score.py, para usar os pesos calibrados:
    w = load_calibrated_weights()
    peso_rsi = w.get("rsi", 10)   # cai no default se não houver calibração

IMPORTANTE: sem no mínimo ~100 trades reais a calibração não é confiável e a
função recusa executar — evita trocar um overfitting manual por outro
estatístico com amostra insuficiente.
"""
import json
import os
from typing import Optional

import numpy as np

from bot.logger import log

# Arquivo onde os pesos calibrados são persistidos
WEIGHTS_FILE = os.environ.get("SCORE_WEIGHTS_FILE", "score_weights.json")

# Pesos padrão (os valores manuais atuais de score.py — usados como fallback)
DEFAULT_WEIGHTS = {
    "trend_align":   10,
    "rsi":            8,
    "volume":         8,
    "macd":           6,
    "orderflow":      6,
    "structure":      5,
    "regime":         5,
    "funding":        4,
    "oi_delta":       4,
    "news":           3,
    "correlation":    3,
}


def load_calibrated_weights() -> dict:
    """
    Carrega os pesos calibrados do disco. Se não existirem (ou se o arquivo
    estiver corrompido), retorna os pesos manuais padrão — o bot continua
    funcionando normalmente.
    """
    try:
        if os.path.exists(WEIGHTS_FILE):
            with open(WEIGHTS_FILE) as f:
                data = json.load(f)
            weights = data.get("weights", {})
            if weights:
                log.info(
                    f"📐 Pesos calibrados carregados "
                    f"(n={data.get('n_trades', '?')} trades, "
                    f"auc={data.get('auc', 0):.3f})"
                )
                return {**DEFAULT_WEIGHTS, **weights}
    except Exception as e:
        log.warning(f"load_calibrated_weights: {e} — usando pesos padrão")
    return dict(DEFAULT_WEIGHTS)


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _fit_logistic(X: np.ndarray, y: np.ndarray,
                  epochs: int = 3000, lr: float = 0.05,
                  l2: float = 0.01) -> np.ndarray:
    """
    Regressão logística com gradiente descendente e regularização L2.
    Implementada em NumPy puro para não adicionar dependência (sklearn).
    L2 é essencial aqui: com poucas amostras, evita que um componente
    domine os pesos por ruído.
    """
    n, d = X.shape
    w = np.zeros(d)
    b = 0.0
    for _ in range(epochs):
        z     = X @ w + b
        p     = _sigmoid(z)
        err   = p - y
        grad_w = (X.T @ err) / n + l2 * w
        grad_b = err.mean()
        w -= lr * grad_w
        b -= lr * grad_b
    return w


def _auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """AUC-ROC via contagem de pares concordantes (Mann-Whitney U)."""
    pos = y_score[y_true == 1]
    neg = y_score[y_true == 0]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    # Ranking-based (equivalente à estatística U normalizada)
    all_s  = np.concatenate([pos, neg])
    ranks  = all_s.argsort().argsort().astype(float) + 1
    r_pos  = ranks[: len(pos)].sum()
    u      = r_pos - len(pos) * (len(pos) + 1) / 2
    return float(u / (len(pos) * len(neg)))


async def calibrate_from_history(min_trades: int = 100,
                                  test_ratio: float = 0.30) -> dict:
    """
    Deriva os pesos do Entry Score a partir dos trades históricos.

    Processo:
      1. Carrega trades da base (features do score + resultado win/loss)
      2. Split temporal treino/teste (sem embaralhar — evita look-ahead)
      3. Ajusta regressão logística no treino
      4. Mede AUC out-of-sample no teste
      5. Só persiste os pesos se AUC > 0.55 (melhor que aleatório)

    Retorna dict com weights, auc, n_trades e status.
    """
    try:
        from bot import database as db
        trades = await db.get_trades_with_features()
    except Exception as e:
        return {"status": "error", "reason": f"leitura da base falhou: {e}"}

    if not trades or len(trades) < min_trades:
        n = len(trades) if trades else 0
        msg = (
            f"amostra insuficiente: {n} trades (mínimo {min_trades}). "
            f"Pesos manuais mantidos — calibrar com poucos dados apenas "
            f"trocaria um overfitting por outro."
        )
        log.warning(f"📐 Calibração abortada — {msg}")
        return {"status": "insufficient_data", "n_trades": n, "reason": msg}

    feature_names = list(DEFAULT_WEIGHTS.keys())
    X_rows, y_rows = [], []
    for t in trades:
        feats = t.get("score_features") or {}
        if isinstance(feats, str):
            try:
                feats = json.loads(feats)
            except Exception:
                continue
        if not feats:
            continue
        X_rows.append([float(feats.get(f, 0)) for f in feature_names])
        y_rows.append(1 if float(t.get("pnl", 0)) > 0 else 0)

    if len(X_rows) < min_trades:
        return {
            "status": "insufficient_features",
            "n_trades": len(X_rows),
            "reason": "trades sem score_features registradas",
        }

    X = np.array(X_rows, dtype=float)
    y = np.array(y_rows, dtype=float)

    # Normalização (z-score) — pesos comparáveis entre features
    mu    = X.mean(axis=0)
    sigma = X.std(axis=0)
    sigma[sigma == 0] = 1.0
    Xn = (X - mu) / sigma

    # Split TEMPORAL (não aleatório): treina no passado, testa no futuro
    split   = int(len(Xn) * (1 - test_ratio))
    X_train, X_test = Xn[:split], Xn[split:]
    y_train, y_test = y[:split], y[split:]

    if len(X_test) < 20 or len(np.unique(y_test)) < 2:
        return {
            "status": "insufficient_test",
            "reason": "conjunto de teste pequeno ou sem ambas as classes",
        }

    w = _fit_logistic(X_train, y_train)
    auc = _auc(y_test, X_test @ w)

    log.info(f"📐 Calibração: AUC out-of-sample = {auc:.3f} (n={len(X_rows)})")

    if auc <= 0.55:
        msg = (
            f"AUC {auc:.3f} <= 0.55 — os componentes do score não demonstram "
            f"poder preditivo out-of-sample. Pesos manuais mantidos."
        )
        log.warning(f"📐 {msg}")
        return {"status": "no_edge", "auc": auc, "n_trades": len(X_rows), "reason": msg}

    # Converte coeficientes em pesos positivos na mesma escala dos manuais
    coef      = np.abs(w)
    total     = coef.sum() or 1.0
    scale     = sum(DEFAULT_WEIGHTS.values())
    weights   = {
        name: round(float(c / total * scale), 2)
        for name, c in zip(feature_names, coef)
    }

    payload = {
        "weights":  weights,
        "auc":      round(float(auc), 4),
        "n_trades": len(X_rows),
        "status":   "calibrated",
    }
    try:
        with open(WEIGHTS_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        log.info(f"📐 Pesos calibrados salvos em {WEIGHTS_FILE}: {weights}")
    except Exception as e:
        log.error(f"Falha ao salvar pesos: {e}")

    return payload
