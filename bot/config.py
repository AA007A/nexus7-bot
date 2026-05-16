import os

class Config:
    # Bybit API
    API_KEY:    str = os.environ.get("BYBIT_API_KEY",    os.environ.get("BINANCE_API_KEY",    ""))
    API_SECRET: str = os.environ.get("BYBIT_API_SECRET", os.environ.get("BINANCE_API_SECRET", ""))

    # Símbolos monitorados — ampliado para selecionar os melhores
    SYMBOLS: list = [
        "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
        "DOGEUSDT","LINKUSDT","ADAUSDT","MATICUSDT","DOTUSDT",
        "LTCUSDT","ATOMUSDT","NEARUSDT","APTUSDT",
        "ARBUSDT","OPUSDT","SUIUSDT","AVAXUSDT",
        "WIFUSDT","1000BONKUSDT",
    ]

    LEVERAGE:        int   = int(os.environ.get("LEVERAGE",        "5"))
    MAX_RISK_PCT:    float = float(os.environ.get("MAX_RISK_PCT",  "0.30"))  # 30% do poder por trade
    MAX_DRAWDOWN:    float = float(os.environ.get("MAX_DRAWDOWN",  "0.15"))  # 15% DD máximo → para
    MAX_POSITIONS:   int   = int(os.environ.get("MAX_POSITIONS",   "3"))     # máximo 3 simultâneas
    MIN_ENTRY_SCORE: int   = int(os.environ.get("MIN_ENTRY_SCORE", "80"))    # score mínimo 0-100
    MIN_CONFIDENCE:  float = float(os.environ.get("MIN_CONFIDENCE","0.80"))  # 80% confiança

    # Trailing stop progressivo — ativado por milestones de 10%
    # Quando lucro >= 10% → SL em +5%, >= 20% → SL em +10%, etc.
    TRAILING_STEP:   float = 0.10   # milestone de 10% de lucro
    TRAILING_LOCK:   float = 0.50   # trava 50% do milestone (metade do ganho)

    # Notificações Telegram (opcionais)
    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT",  "")

    # Sistema
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))

cfg = Config()
