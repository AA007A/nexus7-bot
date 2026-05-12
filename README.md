# ⬡ NEXUS-7 — AI Trading Bot v5.0

Bot de trading automático multi-símbolo para Bybit Futures.

## Variáveis de Ambiente (Railway)

| Variável | Valor |
|----------|-------|
| `BYBIT_API_KEY` | Sua API Key da Bybit |
| `BYBIT_API_SECRET` | Seu Secret da Bybit |
| `LEVERAGE` | `5` (recomendado) |
| `MAX_RISK_PCT` | `0.01` (1% por trade) |
| `MAX_DRAWDOWN` | `0.08` (para com 8% de perda) |
| `MAX_POSITIONS` | `3` (posições simultâneas) |
| `INITIAL_CAP` | Valor inicial da sua banca |
| `TELEGRAM_TOKEN` | Token do bot Telegram (opcional) |
| `TELEGRAM_CHAT` | Chat ID Telegram (opcional) |

## Endpoints

- `GET /` — Status
- `GET /health` — Health check
- `GET /api/status` — Status detalhado do bot
- `GET /api/balance` — Saldo atual
- `POST /api/pause` — Pausa o bot
- `POST /api/resume` — Retoma o bot
- `GET /dashboard` — Interface visual

## Pares monitorados
BTC, ETH, SOL, BNB, XRP (USDT Perpetual)
