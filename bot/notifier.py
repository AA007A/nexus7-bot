import aiohttp
from bot.config import cfg
from bot.logger import log


async def notify(text: str):
    """Envia mensagem para o Telegram."""
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={
                "chat_id": cfg.TELEGRAM_CHAT,
                "text": text,
                "parse_mode": "Markdown",
            })
    except Exception as e:
        log.warning(f"Telegram: {e}")


# ── BOT ONLINE ────────────────────────────────────────────────────
async def online_msg(saldo: float, poder: float, pares: int, max_pos: int) -> str:
    return (
        f"🤖 *AA Capital — ONLINE*\n"
        f"`{'━'*28}`\n"
        f"💰 Saldo:        `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`\n"
        f"🔍 Pares:        `{pares} ativos`\n"
        f"📊 Max posições: `{max_pos}`\n"
        f"`{'━'*28}`\n"
        f"_Bot iniciado e escaneando o mercado..._"
    )


# ── SINAL DE ENTRADA ──────────────────────────────────────────────
async def signal_msg(sig) -> str:
    icon = "🟢🚀" if sig.direction == "LONG" else "🔴🩸"
    dir_label = "COMPRA (LONG)" if sig.direction == "LONG" else "VENDA (SHORT)"
    return (
        f"{icon} *SINAL — {dir_label}*\n"
        f"`{'━'*28}`\n"
        f"📍 Par:          `{sig.symbol}`\n"
        f"💰 Entrada:      `${sig.entry:,.4f}`\n"
        f"🛑 Stop Loss:    `${sig.sl:,.4f}`\n"
        f"🎯 Take Profit:  `${sig.tp:,.4f}`\n"
        f"📊 R/R:          `1:{sig.rr:.1f}`\n"
        f"🧠 Score:        `{sig.score}/100`\n"
        f"💡 _{sig.reason}_\n"
        f"`{'━'*28}`\n"
        f"⚡ _Ordem enviada para a Bybit_"
    )


# ── ORDEM ABERTA ──────────────────────────────────────────────────
async def order_opened_msg(sig, qty: float, saldo: float, poder: float) -> str:
    icon = "🟢" if sig.direction == "LONG" else "🔴"
    return (
        f"{icon} *ORDEM ABERTA*\n"
        f"`{'━'*28}`\n"
        f"📍 Par:          `{sig.symbol}`\n"
        f"🧭 Direção:      `{sig.direction}`\n"
        f"💰 Entrada:      `${sig.entry:,.4f}`\n"
        f"📦 Quantidade:   `{qty}`\n"
        f"🛑 Stop Loss:    `${sig.sl:,.4f}`\n"
        f"🎯 Take Profit:  `${sig.tp:,.4f}`\n"
        f"📊 R/R:          `1:{sig.rr:.1f}`\n"
        f"🧠 Score:        `{sig.score}/100`\n"
        f"`{'━'*28}`\n"
        f"💼 Saldo atual:  `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`"
    )


# ── TRADE FECHADO ─────────────────────────────────────────────────
async def close_msg(symbol: str, direction: str, pnl: float, pnl_pct: float,
                    exit_price: float, saldo: float = 0, poder: float = 0) -> str:
    icon = "💰✅" if pnl > 0 else "📉❌"
    resultado = "LUCRO" if pnl > 0 else "PREJUÍZO"
    return (
        f"{icon} *TRADE FECHADO — {resultado}*\n"
        f"`{'━'*28}`\n"
        f"📍 Par:          `{symbol}`\n"
        f"🧭 Direção:      `{direction}`\n"
        f"🏁 Saída:        `${exit_price:,.4f}`\n"
        f"💵 PnL:          `{'+' if pnl >= 0 else ''}${pnl:,.2f} USDT`\n"
        f"📈 PnL %:        `{'+' if pnl_pct >= 0 else ''}{pnl_pct:,.2f}%`\n"
        f"`{'━'*28}`\n"
        f"💼 Saldo atual:  `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`\n"
        f"_Operação finalizada_"
    )


# ── RELATÓRIO DIÁRIO ──────────────────────────────────────────────
async def daily_report_msg(pnl_dia: float, saldo: float, poder: float,
                            trades: int, wins: int, meta: float, stop: float) -> str:
    icon = "📈" if pnl_dia >= 0 else "📉"
    win_rate = round(wins / trades * 100) if trades > 0 else 0
    pct_meta = round(pnl_dia / meta * 100) if meta > 0 else 0
    return (
        f"{icon} *RELATÓRIO DIÁRIO — AA Capital*\n"
        f"`{'━'*28}`\n"
        f"💵 PnL do dia:   `{'+' if pnl_dia >= 0 else ''}${pnl_dia:,.2f} USDT`\n"
        f"🎯 Meta diária:  `${meta:,.0f}` ({pct_meta}% atingido)\n"
        f"📊 Trades:       `{trades}` ({wins} wins — {win_rate}% win rate)\n"
        f"`{'━'*28}`\n"
        f"💼 Saldo:        `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`\n"
        f"🛑 Stop diário:  `${stop:,.0f}`\n"
        f"_Próximo relatório em 24h_"
    )


# ── META DIÁRIA ───────────────────────────────────────────────────
async def daily_target_msg(pnl: float, meta: float, saldo: float, poder: float) -> str:
    return (
        f"🎯 *META DIÁRIA BATIDA!*\n"
        f"`{'━'*28}`\n"
        f"💵 Lucro do dia: `+${pnl:,.2f} USDT`\n"
        f"🏆 Meta:         `${meta:,.0f} USDT`\n"
        f"💼 Saldo:        `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`\n"
        f"`{'━'*28}`\n"
        f"_Modo conservador ativado até meia-noite UTC_"
    )


# ── STOP LOSS DIÁRIO ──────────────────────────────────────────────
async def daily_stop_msg(pnl: float, stop: float, saldo: float) -> str:
    return (
        f"🛑 *STOP-LOSS DIÁRIO ATINGIDO*\n"
        f"`{'━'*28}`\n"
        f"📉 Perda do dia: `${pnl:,.2f} USDT`\n"
        f"🚫 Limite:       `-${stop:,.0f} USDT`\n"
        f"💼 Saldo:        `${saldo:,.2f} USDT`\n"
        f"`{'━'*28}`\n"
        f"_Bot pausado até meia-noite UTC_"
    )


# ── DRAWDOWN ──────────────────────────────────────────────────────
async def drawdown_msg(dd_pct: float, saldo: float) -> str:
    return (
        f"⚠️ *DRAWDOWN ELEVADO*\n"
        f"`{'━'*28}`\n"
        f"📉 Drawdown:     `{dd_pct:.1%}`\n"
        f"💼 Saldo:        `${saldo:,.2f} USDT`\n"
        f"`{'━'*28}`\n"
        f"_Bot pausado para proteção de capital_"
    )


# ── PERDAS CONSECUTIVAS ───────────────────────────────────────────
async def consecutive_losses_msg(n: int, saldo: float, poder: float) -> str:
    return (
        f"⚠️ *{n} PERDAS CONSECUTIVAS*\n"
        f"`{'━'*28}`\n"
        f"💼 Saldo:        `${saldo:,.2f} USDT`\n"
        f"⚡ Poder compra: `${poder:,.2f} USDT`\n"
        f"`{'━'*28}`\n"
        f"_Bot continua operando com cautela_"
    )

# ── SPOOFING DETECTADO ────────────────────────────────────────────
async def spoofing_alert_msg(symbol: str, detail: str) -> str:
    return (
        f"🚨 *SPOOFING DETECTADO — {symbol}*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"⚠️ Manipulação de orderbook identificada\n"
        f"📋 Detalhe: `{detail}`\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"_Entrada bloqueada por proteção anti-spoofing_"
    )


# ── ICEBERG DETECTADO ─────────────────────────────────────────────
async def iceberg_alert_msg(symbol: str, side: str, price: float) -> str:
    icon = "🐋" 
    return (
        f"{icon} *ICEBERG DETECTADO — {symbol}*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"🔍 Ordem oculta no lado: `{side}`\n"
        f"📍 Nível de preço: `${price:,.4f}`\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"_Grande player identificado neste nível_"
    )


# ── ABSORÇÃO DETECTADA ────────────────────────────────────────────
async def absorption_alert_msg(symbol: str, direction: str,
                                strength: float, detail: str) -> str:
    icon = "🟢" if direction == "BULL" else "🔴"
    return (
        f"{icon} *ABSORÇÃO {direction} — {symbol}*\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"💪 Intensidade: `{strength:.0%}`\n"
        f"📋 `{detail}`\n"
        f"`━━━━━━━━━━━━━━━━━━━━━━━━━━━━`\n"
        f"_Grande player absorvendo pressão oposta_"
    )
