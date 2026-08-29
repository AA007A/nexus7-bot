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
    # O bot usa o formato padrão (BTCUSDT). A conversão para o nome real
    # da KuCoin (XBTUSDTM) é feita dinamicamente em load_instruments(),
    # que consulta /api/v1/contracts/active e descarta pares inexistentes.
    # BNBUSDT foi removido: a KuCoin não oferece futures de BNB.
    SYMBOLS: list = [
        "BTCUSDT",  "ETHUSDT",  "SOLUSDT",  "XRPUSDT",
        "ADAUSDT",  "DOGEUSDT", "LINKUSDT", "AVAXUSDT",
        "DOTUSDT",  "LTCUSDT",  "NEARUSDT", "ATOMUSDT",
    ]

    # ── Risco ─────────────────────────────────────────────────────
    # CORRIGIDO (auditoria #1 — CRÍTICO, pendente havia 3 rodadas de revisão):
    # a configuração anterior era LEVERAGE=50 + MAX_RISK_PCT=1.0 (100% do
    # saldo como margem por posição). O próprio comentário original já
    # admitia: "liquidação ocorre a ~2% de movimento adverso, com folga de
    # apenas 0.5% até o SL". Um único wick em altcoin ilíquido (DOGE, LINK,
    # AVAX, DOT — todos na lista SYMBOLS) era suficiente para zerar a conta.
    #
    # Novos defaults: risco de ~2% do saldo por trade, alavancagem 10x.
    #   notional = balance × LEVERAGE × MAX_RISK_PCT
    #   margem   = notional / LEVERAGE = balance × MAX_RISK_PCT
    # Com MAX_RISK_PCT=0.02 → margem = 2% do saldo por posição, e o SL
    # técnico (não a liquidação) volta a ser o fator que decide o trade.
    # Ainda são valores agressivos para padrão de mesa institucional
    # (1% é mais comum) — ajuste via env var LEVERAGE/MAX_RISK_PCT para
    # o seu apetite de risco real antes de operar capital que você não
    # pode perder.
    LEVERAGE:        int   = int(os.environ.get("LEVERAGE",        "10"))
    MAX_RISK_PCT:    float = float(os.environ.get("MAX_RISK_PCT",  "0.02"))
    # Cap de margem: com sizing agora em ~2% do saldo por trade, não há mais
    # necessidade de reservar quase 100% da conta para uma única posição.
    # 0.50 dá bastante folga para múltiplas posições + variação de preço
    # entre o cálculo e a execução.
    MAX_MARGIN_PCT:  float = float(os.environ.get("MAX_MARGIN_PCT", "0.50"))
    # Drawdown 15%: com sizing de ~2%/trade o bot aguenta uma sequência de
    # perdas real antes de pausar, sem precisar do colchão de 40% que só
    # existia para compensar o sizing de 100% anterior.
    MAX_DRAWDOWN:    float = float(os.environ.get("MAX_DRAWDOWN",  "0.15"))
    INITIAL_CAP:     float = float(os.environ.get("INITIAL_CAP",  "0"))
    # MAX_POSITIONS: com sizing de ~2%/trade (em vez de 100%) já sobra
    # margem para mais de uma posição simultânea. Mantido em 1 nesta
    # correção para não alterar o comportamento de diversificação/
    # correlação sem você validar em paper trade primeiro — pode subir
    # para 2-3 com segurança depois de confirmar o novo sizing em produção.
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
    # CORRIGIDO (auditoria #1): os 20%/20% anteriores só faziam sentido
    # porque um único SL, com sizing de 100% do saldo, já valia ~15% do
    # capital. Com MAX_RISK_PCT=0.02, um SL individual vale ~2% do saldo —
    # um stop diário de 20% deixaria de funcionar como proteção (o bot
    # poderia levar ~10 perdas seguidas no mesmo dia sem pausar).
    # Ajustado para um perfil mais próximo do padrão de mesa: stop diário
    # de 5% (≈2-3 SLs seguidos) e meta diária de 10%.
    DAILY_TARGET_PCT:    float = float(os.environ.get("DAILY_TARGET_PCT",    "0.10"))
    DAILY_STOP_LOSS_PCT: float = float(os.environ.get("DAILY_STOP_LOSS_PCT", "0.05"))
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
