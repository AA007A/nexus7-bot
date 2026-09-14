# NEXUS-7 startup notification state machine

This change is observability-only and does not grant execution permission or alter trading logic.

1. During LIVE bootstrap, the first Telegram banner is transitional: `NEXUS-7 INICIALIZANDO`.
2. The transitional message never labels the engine as disconnected/inactive because those flags are expected to be false before the runtime finishes bootstrapping.
3. A deferred read-only task observes the existing engine state and waits for stable connected/active readiness plus initialized symbols/risk context.
4. After readiness stabilizes, one definitive banner is sent:
   - `LIVE OPERACIONAL` if no observed entry blocker exists;
   - `LIVE BLOQUEADO` with the observed blocker(s), including `DRAWDOWN_HARD_GATE` and drawdown/limit percentages when applicable.
5. If readiness does not settle within the bounded startup window, the notifier emits `INICIALIZAÇÃO NÃO CONCLUÍDA` and does not claim order availability.

No leverage, sizing, strategy, exchange mutation, risk threshold, or execution authorization is changed by this state machine.
