import aiohttp
from bot.config import cfg
from bot.logger import log


async def notify(text: str):
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


async def signal_msg(sig) -> str:
    icon = "🟢🚀" if sig.direction == "LONG" else "🔴🩸"
    return (
        f"{icon} *KAKAZITO TRADE — {sig.direction}*\n"
        f"`{'━'*26}`\n"
        f"📍 Par:     `{sig.symbol}`\n"
        f"💰 Entrada: `${sig.entry:,.2f}`\n"
        f"🛑 Stop:    `${sig.sl:,.2f}`\n"
        f"🎯 Target:  `${sig.tp:,.2f}`\n"
        f"📊 R/R:     `1:{sig.rr}`\n"
        f"🧠 Conf:    `{sig.confidence:.0%}`\n"
        f"💡 _{sig.reason}_\n"
        f"`{'━'*26}`\n"
        f"⚠️ _Não é aconselhamento financeiro_"
    )
