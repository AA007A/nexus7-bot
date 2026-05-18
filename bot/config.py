import os

class Config:
    # ── API ──────────────────────────────────────────────────────
    API_KEY:    str = os.environ.get("BYBIT_API_KEY",    os.environ.get("BINANCE_API_KEY",    ""))
    API_SECRET: str = os.environ.get("BYBIT_API_SECRET", os.environ.get("BINANCE_API_SECRET", ""))

    # ── Símbolos monitorados ─────────────────────────────────────
    SYMBOLS: list = [
        "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
        "LINKUSDT","AVAXUSDT","DOTUSDT","ADAUSDT","MATICUSDT",
        "ARBUSDT","NEARUSDT","APTUSDT","OPUSDT","ATOMUSDT",
    ]

    # ── Risk ─────────────────────────────────────────────────────
    LEVERAGE:        int   = int(os.environ.get("LEVERAGE",        "50"))   # 10x máximo
    MAX_RISK_PCT:    float = float(os.environ.get("MAX_RISK_PCT",  "0.20")) # 20% por trade
    MAX_DRAWDOWN:    float = float(os.environ.get("MAX_DRAWDOWN",  "0.15")) # 15% DD máximo
    MAX_POSITIONS:   int   = int(os.environ.get("MAX_POSITIONS",   "5"))    # 5 simultâneas

    # ── Qualidade de entrada ─────────────────────────────────────
    MIN_ENTRY_SCORE: int   = int(os.environ.get("MIN_ENTRY_SCORE", "80"))   # 90/100 mínimo
    MIN_VOLUME_MULT: float = float(os.environ.get("MIN_VOLUME_MULT","1.5")) # vol > 1.5x média

    # ── Filtro de taxas ──────────────────────────────────────────
    FEE_MULTIPLIER:  float = float(os.environ.get("FEE_MULTIPLIER", "3.0")) # lucro >= 4x taxas

    # ── Timeframes ───────────────────────────────────────────────
    TF_TREND:  str = "240"   # 4H — tendência principal
    TF_CONF:   str = "60"    # 1H — confirmação
    TF_ENTRY:  str = "15"    # 15M — timing de entrada

    # ── SL / TP (em múltiplos de ATR) ───────────────────────────
    SL_ATR_MULT: float = float(os.environ.get("SL_ATR_MULT", "2.0"))  # SL = 2x ATR 1H
    TP_ATR_MULT: float = float(os.environ.get("TP_ATR_MULT", "4.0"))  # TP = 4x ATR 1H (R:R 1:2)

    # ── Trailing stop ────────────────────────────────────────────
    TRAILING_ACTIVATE_PCT: float = 0.08   # só ativa após 8% de lucro
    TRAILING_LOCK_PCT:     float = 0.50   # trava 50% do lucro atual

    # ── Cooldown ─────────────────────────────────────────────────
    COOLDOWN_SECONDS: int = int(os.environ.get("COOLDOWN_SECONDS", "1800"))  # 30 min

    # ── Meta diária ──────────────────────────────────────────────
    DAILY_TARGET:      float = float(os.environ.get("DAILY_TARGET",    "100.0"))
    DAILY_STOP_LOSS:   float = float(os.environ.get("DAILY_STOP_LOSS", "50.0"))
    POST_TARGET_SCORE: int   = int(os.environ.get("POST_TARGET_SCORE", "82"))
    POST_TARGET_RISK:  float = float(os.environ.get("POST_TARGET_RISK","0.20"))

    # ── Sistema ──────────────────────────────────────────────────
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))

    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT",  "")

cfg = Config()
