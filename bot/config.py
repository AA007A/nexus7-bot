import os

class Config:
    # ── API ──────────────────────────────────────────────────────
    API_KEY:    str = os.environ.get("BYBIT_API_KEY",    os.environ.get("BINANCE_API_KEY",    ""))
    API_SECRET: str = os.environ.get("BYBIT_API_SECRET", os.environ.get("BINANCE_API_SECRET", ""))

    # ── Autenticação da API interna ───────────────────────────────
    # Defina BOT_API_SECRET no Railway para proteger os endpoints
    BOT_API_SECRET: str = os.environ.get("BOT_API_SECRET", "")

    # ── Símbolos — pares de alta liquidez e confiabilidade ───────
    SYMBOLS: list = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
        "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT",
        "AVAXUSDT", "POLUSDT", "DOTUSDT", "LTCUSDT",
    ]

    # ── Risk ─────────────────────────────────────────────────────
    # SEGURANÇA: LEVERAGE=10 e MAX_RISK_PCT=0.01 (1% do buying power)
    # Risco real por trade = LEVERAGE × MAX_RISK_PCT = 10 × 1% = 10% do saldo
    # Com LEVERAGE=50 e MAX_RISK_PCT=0.15 o risco era 750% do saldo por trade
    LEVERAGE:        int   = int(os.environ.get("LEVERAGE",        "10"))   # era 50 → CORRIGIDO
    MAX_RISK_PCT:    float = float(os.environ.get("MAX_RISK_PCT",  "0.01")) # era 0.15 → CORRIGIDO: 1% do buying power
    MAX_DRAWDOWN:    float = float(os.environ.get("MAX_DRAWDOWN",  "0.10")) # 10% drawdown máximo
    INITIAL_CAP:     float = float(os.environ.get("INITIAL_CAP",  "0"))
    MAX_POSITIONS:   int   = int(os.environ.get("MAX_POSITIONS",   "3"))    # era 4 → reduzido para controle de correlação
    MIN_CONFIDENCE:  float = float(os.environ.get("MIN_CONFIDENCE","0.75"))
    MIN_RR_RATIO:    float = float(os.environ.get("MIN_RR_RATIO",  "2.0"))  # R/R mínimo 2:1
    TRAILING_TRIGGER:   float = float(os.environ.get("TRAILING_TRIGGER",   "0.50"))  # ativa trailing com 50% do alvo
    TRAILING_LOCK:      float = float(os.environ.get("TRAILING_LOCK",      "0.25"))  # trava 25% abaixo do pico
    # v12: multiplicador do trailing lock em unidades de R (distância ao SL original)
    # lock_distance = R * TRAILING_LOCK_R_MULT
    # Antes: r * 0.5 → lock efetivo de 2.5% (muito apertado para crypto)
    # Agora: r * 1.0 → lock efetivo de 1x o risco → mais espaço para respirar
    TRAILING_LOCK_R_MULT: float = float(os.environ.get("TRAILING_LOCK_R_MULT", "1.0"))

    # ── Correlação entre pares ────────────────────────────────────
    MAX_CORRELATION: float = float(os.environ.get("MAX_CORRELATION", "0.70"))  # bloqueia par se corr > 0.70

    # ── Qualidade de entrada (usado pelo engine.py) ───────────────
    MIN_ENTRY_SCORE:  int   = int(os.environ.get("MIN_ENTRY_SCORE",   "60"))  # score mínimo MTF
    POST_TARGET_SCORE:int   = int(os.environ.get("POST_TARGET_SCORE", "72"))  # após meta diária
    POST_TARGET_RISK: float = float(os.environ.get("POST_TARGET_RISK","0.005")) # era 0.20 → CORRIGIDO: 0.5% do BP
    MIN_VOLUME_MULT:  float = float(os.environ.get("MIN_VOLUME_MULT",  "0.5")) # volume mínimo 0.5x média
    FEE_MULTIPLIER:   float = float(os.environ.get("FEE_MULTIPLIER",   "2.0")) # lucro >= 2x taxas

    # ── Meta diária (em % do saldo) ───────────────────────────────
    # Antes: DAILY_TARGET=100 e DAILY_STOP_LOSS=50 em USD fixo
    # Agora: em percentual do saldo real para escalar com o capital
    DAILY_TARGET_PCT:    float = float(os.environ.get("DAILY_TARGET_PCT",    "0.02"))  # 2% ao dia
    DAILY_STOP_LOSS_PCT: float = float(os.environ.get("DAILY_STOP_LOSS_PCT", "0.01"))  # -1% ao dia (stop diário)
    # Compatibilidade retroativa (valores USD absolutos — usados se saldo=0)
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
    COOLDOWN_SECONDS: int = int(os.environ.get("COOLDOWN_SECONDS", "900"))  # 15 min

    # ── Sistema ──────────────────────────────────────────────────
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))

    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT",  "")

    # ── CORS ─────────────────────────────────────────────────────
    # Liste o domínio real do seu dashboard no Railway (ex: "https://meubot.up.railway.app")
    # Deixe vazio para bloquear tudo (recomendado em produção)
    ALLOWED_ORIGINS: list = [
        o.strip()
        for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    ] or ["http://localhost:3000", "http://localhost:8000"]

cfg = Config()
