import os

class Config:
    # Bybit API
    API_KEY:    str = os.environ.get("BYBIT_API_KEY",    os.environ.get("BINANCE_API_KEY",    ""))
    API_SECRET: str = os.environ.get("BYBIT_API_SECRET", os.environ.get("BINANCE_API_SECRET", ""))

    # Símbolos monitorados
    SYMBOLS: list = [
        "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
        "DOGEUSDT","LINKUSDT","ADAUSDT","MATICUSDT","DOTUSDT",
        "LTCUSDT","ATOMUSDT","NEARUSDT","APTUSDT",
        "ARBUSDT","OPUSDT","SUIUSDT","AVAXUSDT",
        "WIFUSDT","1000BONKUSDT",
    ]

    LEVERAGE:        int   = int(os.environ.get("LEVERAGE",        "50"))
    MAX_RISK_PCT:    float = float(os.environ.get("MAX_RISK_PCT",  "0.50"))  # 50% do poder por trade
    MAX_DRAWDOWN:    float = float(os.environ.get("MAX_DRAWDOWN",  "0.15"))
    MAX_POSITIONS:   int   = int(os.environ.get("MAX_POSITIONS",   "5"))
    MIN_ENTRY_SCORE: int   = int(os.environ.get("MIN_ENTRY_SCORE", "80"))
    MIN_CONFIDENCE:  float = float(os.environ.get("MIN_CONFIDENCE","0.80"))

    # ── META DIÁRIA ──────────────────────────────────────────────
    # Bot opera agressivamente até bater $100/dia, depois fica conservador
    DAILY_TARGET:       float = float(os.environ.get("DAILY_TARGET",        "100.0"))  # meta em USDT
    DAILY_STOP_LOSS:    float = float(os.environ.get("DAILY_STOP_LOSS",     "50.0"))   # stop-loss diário (para tudo se perder $50)
    POST_TARGET_SCORE:  int   = int(os.environ.get("POST_TARGET_SCORE",     "80"))     # score mínimo APÓS bater a meta
    POST_TARGET_RISK:   float = float(os.environ.get("POST_TARGET_RISK",    "0.50"))  # 50% também após meta

    # Trailing stop progressivo
    TRAILING_STEP:   float = 0.10
    TRAILING_LOCK:   float = 0.50

    # Notificações Telegram (opcionais)
    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT",  "")

    # Sistema
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))

cfg = Config()
