# BGX Capital — Changelog

## v11.0.0 — Melhorias Críticas de Risco e Segurança

### 🔴 Correções Críticas (Risco de Perda de Capital)

#### Parâmetros de Risco — CORRIGIDO
- **LEVERAGE**: `50` → `10` (era: risco por trade = 750% do saldo)
- **MAX_RISK_PCT**: `0.15` → `0.01` (1% do buying power)
- **Risco real por trade**: `750%` → `10%` do saldo
- **MAX_POSITIONS**: `4` → `3` (para controle de correlação)
- **POST_TARGET_RISK**: `0.20` → `0.005` (corrigido proporcional)

#### Bug `_update_balance` — CORRIGIDO
- `_dbal` era `NameError` silenciado por `except: pass`
- Notificações de drawdown **nunca eram enviadas**
- Corrigido: `_dbal = bal` inicializado antes do bloco condicional

#### Bug `stop()` / Pause-Resume — CORRIGIDO
- `stop()` agora seta `self._running = False` (antes só `active = False`)
- Bot ficava preso após primeiro pause — impossível retomar sem restart
- `resume` verifica se task está viva antes de criar nova (evita double-engine)

#### Bug `_check_rr_double` — CORRIGIDO
- Taxa era recalculada duas vezes (código morto sobrescrevia variáveis)
- Removida duplicação: `fee_open`/`fee_close` calculados uma única vez

### 🟠 Correções de Segurança

#### Autenticação nos Endpoints — ADICIONADO
- Bearer token em todos os endpoints sensíveis (`/api/pause`, `/api/resume`, etc.)
- Configure `BOT_API_SECRET` no Railway para ativar
- Rate limiting: máx 10 req/min por IP nos endpoints de controle

#### CORS Restrito — CORRIGIDO
- `allow_origins=["*"]` → `cfg.ALLOWED_ORIGINS` (configurável via env var)
- Configure `ALLOWED_ORIGINS=https://seudominio.railway.app` no Railway

#### Endpoint de Emergência — ADICIONADO
- `POST /api/close-all`: fecha todas as posições imediatamente
- Para o engine + executa market orders para fechar posições

### 🟡 Correções de Lógica

#### `filters.run_all_filters()` — CONECTADO
- Era código morto (nunca chamado no fluxo real)
- Agora executado no início de `_open()` antes de qualquer cálculo
- Tasks `update_fear_greed` e `update_macro_events` iniciadas no `engine.run()`
- Timeout de 5s por filtro assíncrono (evita travamentos)

#### Trailing Stop — REATIVADO
- Estava deliberadamente desativado (`pass`)
- Reativado com ATR-based progressivo
- Ativa quando lucro >= 50% do alvo (TRAILING_TRIGGER)
- Trava 0.5× R abaixo do pico de preço

#### Meta/Stop Diário em % do Saldo — CORRIGIDO
- `DAILY_TARGET = $100` e `DAILY_STOP_LOSS = $50` (valores fixos)
- Agora: `DAILY_TARGET_PCT = 2%` e `DAILY_STOP_LOSS_PCT = 1%` do saldo real
- Escala automaticamente com o capital (positivo e negativo)
- `_recalc_daily_limits()` chamado a cada atualização de saldo

### 🔵 Novas Funcionalidades

#### Controle de Correlação — NOVO
- Novo módulo `bot/correlation.py`
- Bloqueia abertura de par com correlação > 0.70 com posição já aberta
- Usa retornos percentuais (mais estável que preços absolutos)
- Cache alimentado via WebSocket a cada candle
- Endpoint `GET /api/correlation` no dashboard

#### RSI Completo — CORRIGIDO
- Antes: apenas `out[-1]` calculado, `out[:-1] = 50` (placeholder)
- Agora: todos os índices calculados com Wilder smoothing completo
- Permite: divergências, padrões históricos, cruzamentos de nível

#### Backtesting Robusto — REESCRITO
- **Janela**: 10 dias → **6 meses** (17.280 candles de 15M)
- **Paginação**: busca histórico completo via múltiplas chamadas à API Bybit
- **In-sample/Out-of-sample**: split 80%/20% obrigatório
- **Walk-forward validation**: 4 janelas rolantes (75% treino + 25% teste)
- **Monte Carlo**: 1.000 permutações → probabilidade de ruína
- **Simula TP parcial** 50%/50% e trailing stop no backtesting
- **Métricas**: Sharpe, Sortino, Win Rate, Profit Factor, Max DD, Expectancy

#### Regime Classifier — APRIMORADO
- Novo regime `COMPRESSED` para squeeze de Bollinger + volatilidade muito baixa
- `volatility_rank`: percentil do ATR atual vs 100 períodos
- `size_mult` dinâmico por volatilidade:
  - `vol_rank > 80%` → 70% do tamanho normal (alta volatilidade)
  - `vol_rank < 20%` → 80% do tamanho normal (baixa liquidez)
  - Normal → 100%

#### RiskManager — UNIFICADO
- Dois `RiskManager` com parâmetros contraditórios (`engine.py` e `risk.py`)
- Unificado em `bot/risk.py` — única fonte de verdade
- Classe local no `engine.py` marcada como `DEPRECATED`
- `size()` com cap absoluto: margem nunca excede 80% do saldo

---

## Configuração Necessária no Railway

Adicione estas variáveis de ambiente:
```
BOT_API_SECRET=<token-secreto-forte>
ALLOWED_ORIGINS=https://seu-dashboard.railway.app
LEVERAGE=10
MAX_RISK_PCT=0.01
MAX_DRAWDOWN=0.10
DAILY_TARGET_PCT=0.02
DAILY_STOP_LOSS_PCT=0.01
MAX_CORRELATION=0.70
```
