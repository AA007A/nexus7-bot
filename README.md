# 🤖 BGX Capital — Trading Bot

Bot de trading automatizado para futuros Bybit com análise multi-timeframe, gestão de risco avançada e alertas em tempo real.

## ⚡ Funcionalidades

- **Análise Multi-Timeframe** — 4H → 1H → 15M com score 0-100
- **12 indicadores** — ADX, EMA, RSI, MACD, VWAP, Bollinger, Choppiness, SMC, CVD, OI, Funding Rate, Fear & Greed
- **Gestão de risco automática** — Meta diária, Stop diário, Drawdown máximo, Trailing Stop
- **Paper Trading** — Teste sem risco real (`PAPER_TRADE=true`)
- **Alertas Telegram** — Sinal, ordem aberta, trade fechado, relatório diário
- **Dashboard ao vivo** — Gráfico PnL, Scan Log, posições, métricas
- **Backtest semanal** — Win Rate, Sharpe, Sortino, Profit Factor

## 🚀 Deploy

Hospedado no [Railway](https://railway.app). A cada push na `main`, o Railway faz redeploy automático.

## ⚙️ Variáveis de Ambiente (Railway)

| Variável | Descrição | Exemplo |
|---|---|---|
| `BYBIT_API_KEY` | Chave API Bybit | `abc123...` |
| `BYBIT_API_SECRET` | Secret API Bybit | `xyz789...` |
| `TELEGRAM_TOKEN` | Token do bot Telegram | `123456:ABC...` |
| `TELEGRAM_CHAT` | ID do chat/grupo | `5059768630` |
| `LEVERAGE` | Alavancagem (padrão: 50) | `50` |
| `MAX_POSITIONS` | Posições simultâneas (padrão: 4) | `4` |
| `MIN_ENTRY_SCORE` | Score mínimo para entrar (padrão: 60) | `60` |
| `DAILY_TARGET` | Meta diária em USDT (padrão: 100) | `100` |
| `DAILY_STOP_LOSS` | Stop diário em USDT (padrão: 50) | `50` |
| `PAPER_TRADE` | Simula sem ordens reais | `true` |

## 📱 Comandos Telegram

| Comando | Descrição |
|---|---|
| `/status` | Status completo do bot |
| `/balance` | Saldo e poder de compra |
| `/positions` | Posições abertas |
| `/pnl` | Lucro/prejuízo da sessão |
| `/pause` | Pausar o bot |
| `/resume` | Retomar o bot |
| `/help` | Lista de comandos |

## 📊 Dashboard

Acesse: `https://nexus7-bot-production.up.railway.app/dashboard`

## 🛡️ Gestão de Risco

- **Score mínimo**: 60/100 para entrar (88/100 após meta diária)
- **R:R mínimo**: 2:1
- **Max posições**: 4 simultâneas
- **Trailing Stop**: ativa ao atingir 50% do alvo
- **Stop diário**: para tudo ao perder $50/dia
- **Drawdown máximo**: 80%

## 📁 Estrutura

```
├── main.py              # FastAPI + endpoints REST
├── bot/
│   ├── engine.py        # Motor principal de trading
│   ├── strategy.py      # Análise técnica MTF
│   ├── score.py         # Sistema de score pré-trade
│   ├── bybit.py         # Cliente Bybit (REST + WebSocket)
│   ├── notifier.py      # Alertas Telegram
│   ├── database.py      # PostgreSQL / SQLite
│   ├── indicators.py    # Indicadores técnicos
│   ├── market_data.py   # CVD, Macro, Heatmap
│   ├── backtest.py      # Backtesting engine
│   └── config.py        # Configurações
└── dashboard/
    └── index.html       # Dashboard web
```

---
*BGX Capital — Automated Trading System*
