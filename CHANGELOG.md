# BGX Capital — Changelog

## v12.0.0 — Melhorias de Robustez, Performance e Arquitetura

### 🔴 Curto Prazo — Bugs e Configuração

#### Item 2 — MATICUSDT → POLUSDT — CORRIGIDO
- `bot/config.py`: Par renomeado pela Bybit em outubro/2024
- Evita erros silenciosos no scan e tentativas de operar par inexistente

#### Item 3 — `_ws_retry` backoff exponencial — CORRIGIDO
- `bot/bybit.py`: `_ws_retry` agora incrementado a cada falha de conexão WS
- Backoff: `wait = min(2^retry, 60)` segundos (era sempre 5s)
- Evita flood de conexões em queda do servidor Bybit

#### Item 4 — `__import__` dinâmicos removidos do hot path — CORRIGIDO
- `bot/engine.py`: 8 chamadas `__import__` no loop de 5s eliminadas
- Substituídas por imports estáticos: `_np`, `_dt`, `_ema_fn`
- Impacto: elimina 69.120+ lookups de módulo por hora

#### Item 5 — `check_partial_tps` conectado ao engine — CORRIGIDO
- `bot/engine.py`: `check_partial_tps` de `bot/risk.py` agora chamado no loop
- TPs parciais 50%/50% funcionam de fato (era código morto)
- TP1: fecha 50% + move SL para break-even
- TP2: fecha 50% restante

#### Item 6 — `MIN_SCORE` alinhado com `cfg.MIN_ENTRY_SCORE` — CORRIGIDO
- `bot/score.py`: `MIN_SCORE = cfg.MIN_ENTRY_SCORE` (era hardcoded 50 vs 60 no engine)
- Elimina diagnóstico enganoso nos logs

### 🟡 Médio Prazo

#### Item 7 — `set_sl` respeita `tickSize` — CORRIGIDO
- `bot/bybit.py`: SL arredondado ao múltiplo exato de `tickSize`
- Antes: `round(sl, 6)` rejeitado silenciosamente pela Bybit (ex: BTCUSDT tickSize=0.10)
- Agora: `round(sl / tick) * tick` com formatação de casas decimais correta
- Trailing stop server-side agora efetivamente chega à exchange

#### Item 8 — Proteção contra ordens duplicadas — ADICIONADO
- `bot/bybit.py`: `orderLinkId` gerado como hash MD5 de `symbol+side+qty+timestamp`
- Bybit usa `orderLinkId` como idempotency key — reenvio do mesmo ID não cria posição dupla
- Elimina risco de posição dupla em retry de ordem

#### Item 9 — Cache de filtros inicializado com timestamp válido — CORRIGIDO
- `bot/filters.py`: `ts = time.time()` em vez de `ts = 0`
- Antes: `age_h = (now - 0)/3600 ≈ 488.000h > 25` → filtros F&G e macro inativos no startup
- Agora: filtros aguardam a primeira atualização real das tasks

#### Item 10 — `TRAILING_LOCK` parametrizado e corrigido — CORRIGIDO
- `bot/config.py`: novo parâmetro `TRAILING_LOCK_R_MULT = 1.0` (default)
- `bot/engine.py`: lock = `R × TRAILING_LOCK_R_MULT` (era `peak_price × TRAILING_LOCK × 0.1` = 2.5% efetivo)
- Novo lock efetivo = 1x o risco original → mais espaço para respirar em tendências

#### Item 11 — `PAPER_TRADE` integrado ao estado do engine — CORRIGIDO
- `bot/engine.py`: `self.paper_trade = PAPER_TRADE`
- Visível em `/api/status` → `"paper_trade": true/false`
- Warning no log ao iniciar em modo paper
- Impede que posições paper contaminem RiskManager real

#### Item 12 — Look-ahead bias removido do backtest — CORRIGIDO
- `bot/backtest.py`: `WARMUP = max(WINDOW, 320)` em vez de `WINDOW=100`
- 320 candles de 15M = 20 candles de 4H mínimos antes da primeira iteração
- Elimina sinais inválidos nas primeiras semanas do backtest histórico

### 🔵 Melhorias Avançadas

#### Item 13 — Splitting do engine.py em submódulos — IMPLEMENTADO
- `bot/daily_tracker.py`: DailyTracker class — meta/stop diário desacoplados
- `bot/position_manager.py`: PositionManagerMixin — trailing stop, close all, partial TPs
- `bot/signal_processor.py`: SignalProcessorMixin — helpers de scan e score
- `engine.py`: herda de `PositionManagerMixin` e `SignalProcessorMixin`
- Reduz acoplamento e facilita testes unitários por módulo

#### Item 14 — Filtro de correlação/posições replicado no backtest — IMPLEMENTADO
- `bot/backtest.py`: controle de `MAX_POSITIONS` simultâneas no `_run_strategy`
- Trades rastreados com `status: open/closed` e `close_candle`
- Resultados do backtest mais próximos da operação real

#### Item 15 — Monte Carlo seed dinâmico — CORRIGIDO
- `bot/backtest.py`: `np.random.default_rng(int(time.time_ns()) % 2**32)`
- Era seed fixo=42 → cada run semanal produzia estimativas idênticas
- Agora: estimativas independentes e acumuláveis semana a semana

#### Item 16 — Kelly Criterion fracionado — IMPLEMENTADO
- `bot/backtest.py`: função `kelly_criterion(win_rate, avg_win, avg_loss, fraction=0.25)`
- Fórmula: `K = (W×R - L) / R`, usando 25% do Kelly pleno
- Calculado com métricas out-of-sample e incluído no resultado do backtest
- Cap: máximo de 5% de risco sugerido, independente do Kelly calculado

#### Item 17 — Alertas de degradação e Kelly negativo — IMPLEMENTADO
- `bot/backtest.py`: alerta Telegram se `test_wr < train_wr × 0.80`
- Segundo alerta se Kelly calculado ≤ 0 (estratégia sem expectativa positiva)
- Inclui: janelas afetadas, degradação média, Sharpe OOS, recomendação de ação

---

## Configuração de Variáveis de Ambiente (Railway)

```env
BOT_API_SECRET=<token-secreto-forte>
ALLOWED_ORIGINS=https://seu-dominio.railway.app
LEVERAGE=10
MAX_RISK_PCT=0.01
MAX_DRAWDOWN=0.10
DAILY_TARGET_PCT=0.02
DAILY_STOP_LOSS_PCT=0.01
MAX_CORRELATION=0.70
TRAILING_LOCK_R_MULT=1.0
```
