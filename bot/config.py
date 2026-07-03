import os

class Config:
    # ── API ──────────────────────────────────────────────────────
    API_KEY:    str = os.environ.get("BYBIT_API_KEY",    os.environ.get("BINANCE_API_KEY",    ""))
    API_SECRET: str = os.environ.get("BYBIT_API_SECRET", os.environ.get("BINANCE_API_SECRET", ""))

    # ── Símbolos — pares de alta liquidez e confiabilidade ───────
    SYMBOLS: list = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
        "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT",
        "AVAXUSDT", "MATICUSDT", "DOTUSDT", "LTCUSDT",
    ]

    # ── Risk ─────────────────────────────────────────────────────
    LEVERAGE:        int   = int(os.environ.get("LEVERAGE",        "5"))      # SEGURO: 5x — use env var para aumentar
    MAX_RISK_PCT:    float = float(os.environ.get("MAX_RISK_PCT",  "0.06"))   # 6% para atingir notional mínimo Bybit com saldo pequeno
    MAX_DRAWDOWN:    float = float(os.environ.get("MAX_DRAWDOWN",  "0.08"))   # SEGURO: 8% DD maximo
    INITIAL_CAP:     float = float(os.environ.get("INITIAL_CAP",  "0"))
    MAX_POSITIONS:   int   = int(os.environ.get("MAX_POSITIONS",   "3"))      # SEGURO: max 3 (correlacao entre pares)
    MIN_CONFIDENCE:  float = float(os.environ.get("MIN_CONFIDENCE","0.75"))
    MIN_RR_RATIO:    float = float(os.environ.get("MIN_RR_RATIO",  "2.0"))   # R/R mínimo 2:1
    TRAILING_TRIGGER: float = 0.50
    TRAILING_LOCK:    float = 0.25

    # ── Qualidade de entrada (usado pelo engine.py) ───────────────
    MIN_ENTRY_SCORE:  int   = int(os.environ.get("MIN_ENTRY_SCORE",  "60"))   # score mínimo MTF
    MAX_SPREAD_PCT:   float = float(os.environ.get("MAX_SPREAD_PCT",  "0.05")) # 0.05% spread máximo
    STAGNATION_BARS:  int   = int(os.environ.get("STAGNATION_BARS",  "16"))   # 4h em candles 15M
    STAGNATION_MULT:  float = float(os.environ.get("STAGNATION_MULT","0.5"))  # 0.5xATR sem movimento
    POST_TARGET_SCORE:int   = int(os.environ.get("POST_TARGET_SCORE","72"))   # após meta diária
    POST_TARGET_RISK: float = float(os.environ.get("POST_TARGET_RISK","0.01"))  # SEGURO: 1% pos-meta (conservador)
    MIN_VOLUME_MULT:  float = float(os.environ.get("MIN_VOLUME_MULT", "0.5")) # volume mínimo 0.5x média
    FEE_MULTIPLIER:   float = float(os.environ.get("FEE_MULTIPLIER",  "2.0")) # lucro >= 2x taxas

    # ── Meta diária ──────────────────────────────────────────────
    # Modo dinâmico (recomendado): DAILY_TARGET_PCT e DAILY_STOP_LOSS_PCT
    # Modo fixo (legacy): DAILY_TARGET e DAILY_STOP_LOSS em USD
    DAILY_TARGET:         float = float(os.environ.get("DAILY_TARGET",         "0"))     # 0 = dinâmico
    DAILY_STOP_LOSS:      float = float(os.environ.get("DAILY_STOP_LOSS",      "0"))     # 0 = dinâmico
    DAILY_TARGET_PCT:     float = float(os.environ.get("DAILY_TARGET_PCT",     "0.01"))   # 1% do saldo
    DAILY_STOP_LOSS_PCT:  float = float(os.environ.get("DAILY_STOP_LOSS_PCT",  "0.01"))   # FIX: 0.5%→1% (era restritivo demais)
    WEEKLY_STOP_PCT:      float = float(os.environ.get("WEEKLY_STOP_PCT",      "0.03"))   # 3% por semana
    MONTHLY_STOP_PCT:     float = float(os.environ.get("MONTHLY_STOP_PCT",     "0.08"))   # 8% por mês

    # ── Relatório diário ─────────────────────────────────────────
    REPORT_INTERVAL_H: int = int(os.environ.get("REPORT_INTERVAL_H", "24"))

    # ── Timeframes ───────────────────────────────────────────────
    TF_TREND:  str = "240"   # 4H
    TF_CONF:   str = "60"    # 1H
    TF_ENTRY:  str = "15"    # 15M

    # ── SL / TP ──────────────────────────────────────────────────
    SL_ATR_MULT: float = float(os.environ.get("SL_ATR_MULT", "1.5"))
    TP_ATR_MULT: float = float(os.environ.get("TP_ATR_MULT", "3.0"))

    # ── Cooldown ─────────────────────────────────────────────────
    COOLDOWN_SECONDS: int = int(os.environ.get("COOLDOWN_SECONDS", "900"))  # 15 min pós-trade

    # ── Correlação entre pares ────────────────────────────────────
    # Par com correlação > MAX_CORRELATION com posição aberta é bloqueado
    MAX_CORRELATION: float = float(os.environ.get("MAX_CORRELATION", "0.75"))  # 75% de correlação

    # ── Circuit breaker por ativo ─────────────────────────────────
    MAX_CONSEC_LOSSES:   int = int(os.environ.get("MAX_CONSEC_LOSSES",   "3"))   # perdas → cooldown
    CB_COOLDOWN_HOURS:   int = int(os.environ.get("CB_COOLDOWN_HOURS",   "24"))  # horas de cooldown

    # ── Sistema ──────────────────────────────────────────────────
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))

    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT",  "")

cfg = Config()
