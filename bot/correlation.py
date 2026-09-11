"""
BGX Capital — Correlation Guard v1.1
Bloqueia abertura de novo par se correlação > MAX_CORRELATION
com qualquer posição já aberta.

Usa retornos percentuais (mais estável que preços absolutos).
Cache de closes por símbolo atualizado via WebSocket.

Safety invariant: quando já existe exposição e não há dados suficientes para
provar a correlação do novo símbolo com cada posição aberta, a nova entrada é
bloqueada. Dados ausentes nunca podem significar risco independente.
"""
from bot.logger import log
from bot.config import cfg
from bot.indicators import returns_correlation

# Cache de closes recentes por símbolo (alimentado pelo engine via WS)
_closes_cache: dict = {}   # symbol → list[float]
_CACHE_SIZE = 50            # últimos 50 fechamentos (15M = ~12h)
_MIN_SAMPLES = 20           # janela mínima compatível com returns_correlation(period=20)


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


def _insufficient_data_result(new_symbol: str, missing_symbol: str, samples: int) -> dict:
    reason = (
        f"dados de correlação insuficientes para {missing_symbol}: "
        f"{samples}<{_MIN_SAMPLES} amostras — bloqueado fail-closed"
    )
    log.warning(f"🔗 [Corr] {new_symbol}: {reason}")
    return {
        "ok": False,
        "reason": reason,
        "max_corr": None,
        "correlated_with": missing_symbol,
        "data_quality": "INSUFFICIENT",
    }


def check_correlation(new_symbol: str, open_positions: dict) -> dict:
    """
    Verifica se new_symbol tem correlação alta (> MAX_CORRELATION)
    com qualquer símbolo já em posição aberta.

    Quando não existe posição aberta, correlação não é necessária. Quando já
    existe exposição, TODAS as séries relevantes precisam ter dados suficientes;
    caso contrário a nova entrada é bloqueada por segurança.

    Retorna:
      ok: True se pode abrir | False se bloqueado
      reason: motivo do bloqueio
      max_corr: correlação máxima encontrada
      correlated_with: símbolo mais correlacionado / com dados ausentes
    """
    if not open_positions:
        return {
            "ok": True,
            "reason": "sem posições abertas",
            "max_corr": 0.0,
            "data_quality": "NOT_REQUIRED",
        }

    closes_new = _closes_cache.get(new_symbol, [])
    if len(closes_new) < _MIN_SAMPLES:
        return _insufficient_data_result(new_symbol, new_symbol, len(closes_new))

    max_corr = 0.0
    correlated_with = ""
    compared = 0

    for sym in open_positions:
        if sym == new_symbol:
            continue
        closes_sym = _closes_cache.get(sym, [])
        if len(closes_sym) < _MIN_SAMPLES:
            return _insufficient_data_result(new_symbol, sym, len(closes_sym))

        corr = returns_correlation(closes_new, closes_sym, period=20)
        corr_abs = abs(corr)
        compared += 1

        if corr_abs > max_corr:
            max_corr = corr_abs
            correlated_with = sym

    # Se havia exposição mas nenhuma série pôde ser comparada (por exemplo,
    # estrutura inesperada de open_positions), não inventar independência.
    if compared == 0:
        return _insufficient_data_result(new_symbol, "open_positions", 0)

    threshold = cfg.MAX_CORRELATION
    if max_corr >= threshold:
        reason = (
            f"Correlação {max_corr:.2f} com {correlated_with} "
            f"≥ limite {threshold:.2f} → bloqueado"
        )
        log.info(f"🔗 [Corr] {new_symbol}: {reason}")
        return {
            "ok": False,
            "reason": reason,
            "max_corr": round(max_corr, 3),
            "correlated_with": correlated_with,
            "data_quality": "OK",
        }

    return {
        "ok": True,
        "reason": f"corr_max={max_corr:.2f} < {threshold:.2f} ✓",
        "max_corr": round(max_corr, 3),
        "correlated_with": correlated_with,
        "data_quality": "OK",
    }


def get_correlation_matrix(symbols: list) -> dict:
    """Retorna matriz de correlação entre todos os símbolos fornecidos."""
    matrix = {}
    for s1 in symbols:
        matrix[s1] = {}
        for s2 in symbols:
            if s1 == s2:
                matrix[s1][s2] = 1.0
                continue
            c1 = _closes_cache.get(s1, [])
            c2 = _closes_cache.get(s2, [])
            if len(c1) >= _MIN_SAMPLES and len(c2) >= _MIN_SAMPLES:
                matrix[s1][s2] = round(returns_correlation(c1, c2, 20), 3)
            else:
                matrix[s1][s2] = None
    return matrix
