import os

class Config:
    # Bybit API
    API_KEY:    str = os.environ.get("BYBIT_API_KEY",    os.environ.get("BINANCE_API_KEY",    ""))
    API_SECRET: str = os.environ.get("BYBIT_API_SECRET", os.environ.get("BINANCE_API_SECRET", ""))

    # Trading — pares selecionados, menos posições, maior certeza
    SYMBOLS: list = [
        "XRPUSDT","DOGEUSDT","LINKUSDT","BNBUSDT",
        "ADAUSDT","TRXUSDT","MATICUSDT","DOTUSDT",
        "LTCUSDT","ATOMUSDT","NEARUSDT","APTUSDT",
    ]

    LEVERAGE:        int   = int(os.environ.get("LEVERAGE",       "50"))
    MAX_RISK_PCT:    float = float(os.environ.get("MAX_RISK_PCT", "0.35"))  # 35% do poder por trade (maior)
    MAX_DRAWDOWN:    float = float(os.environ.get("MAX_DRAWDOWN", "0.80"))  # 80% DD máximo
    MAX_POSITIONS:   int   = int(os.environ.get("MAX_POSITIONS",  "4"))     # até 4 posições (menos)
    MIN_CONFIDENCE:  float = float(os.environ.get("MIN_CONFIDENCE","0.75")) # 75% confiança (mais alto)
    MIN_RR_RATIO:    float = float(os.environ.get("MIN_RR_RATIO",  "2.0"))  # Risk/Reward mínimo 2:1
    TRAILING_TRIGGER:float = 0.50   # ativa trailing quando lucro >= 50%
    TRAILING_LOCK:   float = 0.25   # trava 25% do pico de lucro

    # Notificações
    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT",  "")

    # Sistema
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))

cfg = Config()
