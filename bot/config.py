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
    LEVERAGE:        int   = int(os.environ.get("LEVERAGE",        "50"))
    MAX_RISK_PCT:    float = float(os.environ.get("MAX_RISK_PCT",  "0.35"))  # 35% por trade
    MAX_DRAWDOWN:    float = float(os.environ.get("MAX_DRAWDOWN",  "0.80"))  # 80% DD máximo
    INITIAL_CAP:     float = float(os.environ.get("INITIAL_CAP",  "0"))
    MAX_POSITIONS:   int   = int(os.environ.get("MAX_POSITIONS",   "4"))     # até 4 simultâneas
    MIN_CONFIDENCE:  float = float(os.environ.get("MIN_CONFIDENCE","0.75"))
    MIN_RR_RATIO:    float = float(os.environ.get("MIN_RR_RATIO",  "2.0"))   # R/R mínimo 2:1
    TRAILING_TRIGGER: float = 0.50
    TRAILING_LOCK:    float = 0.25

    # ── Qualidade de entrada (usado pelo engine.py) ───────────────
    MIN_ENTRY_SCORE:  int   = int(os.environ.get("MIN_ENTRY_SCORE",  "60"))   # score mínimo MTF
    POST_TARGET_SCORE:int   = int(os.environ.get("POST_TARGET_SCORE","80"))   # após meta diária
    POST_TARGET_RISK: float = float(os.environ.get("POST_TARGET_RISK","0.20"))
    MIN_VOLUME_MULT:  float = float(os.environ.get("MIN_VOLUME_MULT", "1.2")) # volume mínimo 1.2x média
    FEE_MULTIPLIER:   float = float(os.environ.get("FEE_MULTIPLIER",  "2.0")) # lucro >= 2x taxas

    # ── Meta diária ───────────────────────────────────────────────
    DAILY_TARGET:    float = float(os.environ.get("DAILY_TARGET",    "100.0"))
    DAILY_STOP_LOSS: float = float(os.environ.get("DAILY_STOP_LOSS", "50.0"))

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
    COOLDOWN_SECONDS: int = int(os.environ.get("COOLDOWN_SECONDS", "900"))  # 15 min (era 30)

    # ── Sistema ──────────────────────────────────────────────────
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))

    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT",  "")

cfg = Config()
