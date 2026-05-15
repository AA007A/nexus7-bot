import aiohttp
from typing import List
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
        f"{icon} *NEXUS-7 — {sig.direction}*\n"
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


def _pnl_section(label: str, realized_profit: float, realized_loss: float,
                 unrealized: float) -> str:
    net = realized_profit + realized_loss + unrealized
    return (
        f"*{label}*\n"
        f"  ✅ Lucro Garantido:  `${realized_profit:+.2f}`\n"
        f"  ❌ Perda Garantida:  `${realized_loss:+.2f}`\n"
        f"  📈 PnL Não Realizado: `${unrealized:+.2f}`\n"
        f"  💵 Resultado Líquido: `${net:+.2f}`"
    )


def daily_report_msg(
    open_positions: List[dict],
    stats_1d: dict,
    stats_7d: dict,
    stats_30d: dict,
) -> str:
    """
    Formata o relatório diário de performance.

    Cada stats_Xd deve conter:
      realized_profit, realized_loss, unrealized_pnl
    """
    n_open = len(open_positions)
    pos_lines = ""
    if open_positions:
        for p in open_positions:
            icon = "🟢" if p.get("direction") == "LONG" else "🔴"
            pos_lines += (
                f"  {icon} `{p['symbol']}` {p['direction']} "
                f"@ `${p['entry']:.2f}` | PnL: `${p['pnl']:+.2f}`\n"
            )
    else:
        pos_lines = "  _Nenhuma posição aberta_\n"

    sep = f"`{'━'*30}`"
    return (
        f"📊 *NEXUS\\-7 — Relatório Diário*\n"
        f"{sep}\n"
        f"🗂 *Posições Abertas*: `{n_open}/{cfg.MAX_POSITIONS}`\n"
        f"{pos_lines}"
        f"{sep}\n"
        f"{_pnl_section('Últimas 24h', stats_1d['realized_profit'], stats_1d['realized_loss'], stats_1d['unrealized_pnl'])}\n"
        f"{sep}\n"
        f"{_pnl_section('Últimos 7 dias', stats_7d['realized_profit'], stats_7d['realized_loss'], stats_7d['unrealized_pnl'])}\n"
        f"{sep}\n"
        f"{_pnl_section('Últimos 30 dias', stats_30d['realized_profit'], stats_30d['realized_loss'], stats_30d['unrealized_pnl'])}\n"
        f"{sep}\n"
        f"⚠️ _Não é aconselhamento financeiro_"
    )
