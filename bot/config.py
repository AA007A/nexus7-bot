import os


class Config:
    # Bybit API Keys
    API_KEY:    str = os.environ.get("BYBIT_API_KEY",    os.environ.get("BINANCE_API_KEY",    ""))
    API_SECRET: str = os.environ.get("BYBIT_API_SECRET", os.environ.get("BINANCE_API_SECRET", ""))

    # Trading
    SYMBOLS:        list  = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
    LEVERAGE:       int   = int(os.environ.get("LEVERAGE",         "5"))
    MAX_RISK_PCT:   float = float(os.environ.get("MAX_RISK_PCT",   "0.01"))   # 1% por trade
    MAX_DRAWDOWN:   float = float(os.environ.get("MAX_DRAWDOWN",   "0.08"))   # 8% stop total
    INITIAL_CAP:    float = float(os.environ.get("INITIAL_CAP",    "10000"))
    MIN_CONFIDENCE: float = float(os.environ.get("MIN_CONFIDENCE", "0.62"))
    MAX_POSITIONS:  int   = int(os.environ.get("MAX_POSITIONS",   "3"))       # max posições simultâneas

    # Notifications
    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT",  "")

    # System
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))


cfg = Config()
