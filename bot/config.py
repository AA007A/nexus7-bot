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
    MIN_CONFIDENCE:  float = float(os.environ.get("MIN_CONFIDENCE","0.75"))  # 75% confiança mínima
    MIN_RR_RATIO:    float = float(os.environ.get("MIN_RR_RATIO",  "2.0"))   # R/R mínimo 2:1
    TRAILING_TRIGGER: float = 0.50   # ativa trailing quando lucro >= 50%
    TRAILING_LOCK:    float = 0.25   # trava 25% do pico de lucro

    # ── Relatório diário ─────────────────────────────────────────
    REPORT_INTERVAL_H: int = int(os.environ.get("REPORT_INTERVAL_H", "24"))  # horas

    # ── Timeframes ───────────────────────────────────────────────
    TF_TREND:  str = "240"   # 4H — tendência principal
    TF_CONF:   str = "60"    # 1H — confirmação
    TF_ENTRY:  str = "15"    # 15M — timing de entrada

    # ── SL / TP (em múltiplos de ATR) ───────────────────────────
    SL_ATR_MULT: float = float(os.environ.get("SL_ATR_MULT", "1.5"))  # SL = 1.5x ATR
    TP_ATR_MULT: float = float(os.environ.get("TP_ATR_MULT", "3.0"))  # TP = 3x ATR (R:R 1:2)

    # ── Cooldown ─────────────────────────────────────────────────
    COOLDOWN_SECONDS: int = int(os.environ.get("COOLDOWN_SECONDS", "1800"))  # 30 min

    # ── Sistema ──────────────────────────────────────────────────
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))

    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "7763960437:AAHe2sV6icadsF1wddLwbOn1-WZi6LUkaLU")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT",  "8422682029")

cfg = Config()
