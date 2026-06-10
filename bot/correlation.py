"""
BGX Capital — Correlation Guard v1.0
Bloqueia abertura de novo par se correlação > MAX_CORRELATION
com qualquer posição já aberta.

Usa retornos percentuais (mais estável que preços absolutos).
Cache de closes por símbolo atualizado via WebSocket.
"""
from bot.logger import log
from bot.config import cfg
from bot.indicators import returns_correlation

# Cache de closes recentes por símbolo (alimentado pelo engine via WS)
_closes_cache: dict = {}   # symbol → list[float]
_CACHE_SIZE = 50            # últimos 50 fechamentos (15M = ~12h)


def update_closes(symbol: str, close: float):
    """Atualiza cache de closes para um símbolo (chamado a cada kline WS)."""
    if symbol not in _closes_cache:
        _closes_cache[symbol] = []
    _closes_cache[symbol].append(close)
    if len(_closes_cache[symbol]) > _CACHE_SIZE:
        _closes_cache[symbol].pop(0)


def seed_closes(symbol: str, closes: list):
    """Popula cache com histórico inicial (chamado no startup)."""
    _closes_cache[symbol] = list(closes[-_CACHE_SIZE:])


def check_correlation(new_symbol: str, open_positions: dict) -> dict:
    """
    Verifica se new_symbol tem correlação alta (> MAX_CORRELATION)
    com qualquer símbolo já em posição aberta.

    Retorna:
      ok: True se pode abrir | False se bloqueado
      reason: motivo do bloqueio
      max_corr: correlação máxima encontrada
      correlated_with: símbolo mais correlacionado
    """
    if not open_positions:
        return {"ok": True, "reason": "sem posições abertas", "max_corr": 0.0}

    closes_new = _closes_cache.get(new_symbol, [])
    if len(closes_new) < 10:
        # Sem dados suficientes — permite mas avisa
        log.debug(f"[Corr] {new_symbol}: sem dados suficientes — skip check")
        return {"ok": True, "reason": "dados insuficientes — permitindo", "max_corr": 0.0}

    max_corr        = 0.0
    correlated_with = ""

    for sym in open_positions:
        if sym == new_symbol:
            continue
        closes_sym = _closes_cache.get(sym, [])
        if len(closes_sym) < 10:
            continue

        corr = returns_correlation(closes_new, closes_sym, period=20)
        corr_abs = abs(corr)

        if corr_abs > max_corr:
            max_corr        = corr_abs
            correlated_with = sym

    threshold = cfg.MAX_CORRELATION
    if max_corr >= threshold:
        reason = (
            f"Correlação {max_corr:.2f} com {correlated_with} "
            f"≥ limite {threshold:.2f} → bloqueado"
        )
        log.info(f"🔗 [Corr] {new_symbol}: {reason}")
        return {
            "ok":              False,
            "reason":          reason,
            "max_corr":        round(max_corr, 3),
            "correlated_with": correlated_with,
        }

    return {
        "ok":              True,
        "reason":          f"corr_max={max_corr:.2f} < {threshold:.2f} ✓",
        "max_corr":        round(max_corr, 3),
        "correlated_with": correlated_with,
    }


def get_correlation_matrix(symbols: list) -> dict:
    """
    Retorna matriz de correlação entre todos os símbolos fornecidos.
    Usado pelo dashboard para visualização.
    """
    matrix = {}
    for s1 in symbols:
        matrix[s1] = {}
        for s2 in symbols:
            if s1 == s2:
                matrix[s1][s2] = 1.0
                continue
            c1 = _closes_cache.get(s1, [])
            c2 = _closes_cache.get(s2, [])
            if len(c1) >= 10 and len(c2) >= 10:
                matrix[s1][s2] = round(returns_correlation(c1, c2, 20), 3)
            else:
                matrix[s1][s2] = None
    return matrix
