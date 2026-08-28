import os

class Config:
    # ── API KuCoin (substituiu Bybit) ─────────────────────────────
    # Configure no Railway:
    #   KUCOIN_API_KEY, KUCOIN_API_SECRET, KUCOIN_API_PASSPHRASE
    API_KEY:        str = os.environ.get("KUCOIN_API_KEY",        "")
    API_SECRET:     str = os.environ.get("KUCOIN_API_SECRET",     "")
    API_PASSPHRASE: str = os.environ.get("KUCOIN_API_PASSPHRASE", "")

    # ── Autenticação interna da API REST do bot ───────────────────
    BOT_API_SECRET: str = os.environ.get("BOT_API_SECRET", "")

    # ── Símbolos — pares USDT com alta liquidez na KuCoin Futures ─
    # KuCoin usa sufixo M internamente (XBTUSDTM) mas o bot
    # continua usando o formato padrão (BTCUSDT) — conversão
    # é feita automaticamente dentro do KuCoinClient.
    SYMBOLS: list = [
        "BTCUSDT",  "ETHUSDT",  "SOLUSDT",  "BNBUSDT",
        "XRPUSDT",  "ADAUSDT",  "DOGEUSDT", "LINKUSDT",
        "AVAXUSDT", "POLUSDT",  "DOTUSDT",  "LTCUSDT",
    ]

    # ── Risco ─────────────────────────────────────────────────────
    LEVERAGE:        int   = int(os.environ.get("LEVERAGE",        "10"))
    # MAX_RISK_PCT = 1.0 → usa 100% do saldo como margem por posição.
    # notional = balance × LEVERAGE × MAX_RISK_PCT
    # Com balance=$15, LEVERAGE=10, MAX_RISK_PCT=1.0 → notional $150, margem $15.
    # ATENÇÃO: movimento de ~10% contra a posição liquida a conta.
    MAX_RISK_PCT:    float = float(os.environ.get("MAX_RISK_PCT",  "1.0"))
    # Cap de margem: 0.98 = usa 98% do saldo, deixando 2% para taxas.
    # Sem essa folga a exchange rejeita a ordem por saldo insuficiente para fees.
    MAX_MARGIN_PCT:  float = float(os.environ.get("MAX_MARGIN_PCT", "0.98"))
    # Drawdown 40%: com sizing de 100% o bot precisa de espaço para operar.
    # Abaixo disso o bot pausaria após 2-3 trades perdedores.
    MAX_DRAWDOWN:    float = float(os.environ.get("MAX_DRAWDOWN",  "0.40"))
    INITIAL_CAP:     float = float(os.environ.get("INITIAL_CAP",  "0"))
    # MAX_POSITIONS = 1: com 100% do saldo em uma posição não sobra margem
    # para outras. Manter 3 faria as posições 2 e 3 serem rejeitadas.
    MAX_POSITIONS:   int   = int(os.environ.get("MAX_POSITIONS",   "1"))
    MIN_CONFIDENCE:  float = float(os.environ.get("MIN_CONFIDENCE","0.75"))
    MIN_RR_RATIO:    float = float(os.environ.get("MIN_RR_RATIO",  "2.0"))

    # ── Trailing Stop ─────────────────────────────────────────────
    TRAILING_TRIGGER:     float = float(os.environ.get("TRAILING_TRIGGER",     "0.50"))
    TRAILING_LOCK:        float = float(os.environ.get("TRAILING_LOCK",        "0.25"))
    TRAILING_LOCK_R_MULT: float = float(os.environ.get("TRAILING_LOCK_R_MULT", "1.0"))

    # ── Correlação ────────────────────────────────────────────────
    MAX_CORRELATION: float = float(os.environ.get("MAX_CORRELATION", "0.70"))

    # ── Score / Entrada ───────────────────────────────────────────
    MIN_ENTRY_SCORE:   int   = int(os.environ.get("MIN_ENTRY_SCORE",   "60"))
    POST_TARGET_SCORE: int   = int(os.environ.get("POST_TARGET_SCORE", "72"))
    POST_TARGET_RISK:  float = float(os.environ.get("POST_TARGET_RISK","0.005"))
    MIN_VOLUME_MULT:   float = float(os.environ.get("MIN_VOLUME_MULT",  "0.5"))
    FEE_MULTIPLIER:    float = float(os.environ.get("FEE_MULTIPLIER",   "2.0"))

    # ── Meta diária ───────────────────────────────────────────────
    # Com sizing de 100% do saldo, um único SL representa ~15% do capital.
    # Stop diário de 1% travaria o bot no primeiro trade perdedor.
    # Ajustado para 20% — coerente com o perfil de risco escolhido.
    DAILY_TARGET_PCT:    float = float(os.environ.get("DAILY_TARGET_PCT",    "0.20"))
    DAILY_STOP_LOSS_PCT: float = float(os.environ.get("DAILY_STOP_LOSS_PCT", "0.20"))
    DAILY_TARGET:        float = float(os.environ.get("DAILY_TARGET",        "100.0"))
    DAILY_STOP_LOSS:     float = float(os.environ.get("DAILY_STOP_LOSS",     "50.0"))

    # ── Relatório ─────────────────────────────────────────────────
    REPORT_INTERVAL_H: int = int(os.environ.get("REPORT_INTERVAL_H", "24"))

    # ── Timeframes ────────────────────────────────────────────────
    TF_TREND: str = "240"
    TF_CONF:  str = "60"
    TF_ENTRY: str = "15"

    # ── SL / TP ───────────────────────────────────────────────────
    SL_ATR_MULT: float = float(os.environ.get("SL_ATR_MULT", "1.5"))
    TP_ATR_MULT: float = float(os.environ.get("TP_ATR_MULT", "3.0"))

    # ── Cooldown ──────────────────────────────────────────────────
    COOLDOWN_SECONDS: int = int(os.environ.get("COOLDOWN_SECONDS", "900"))

    # ── Sistema ───────────────────────────────────────────────────
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))

    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT",  "")

    # ── CORS ──────────────────────────────────────────────────────
    ALLOWED_ORIGINS: list = [
        o.strip()
        for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    ] or ["http://localhost:3000", "http://localhost:8000"]

cfg = Config()
