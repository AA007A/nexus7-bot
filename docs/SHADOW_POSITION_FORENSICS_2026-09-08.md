# SHADOW Position Forensics — 2026-09-08

This change is observability-only.

It logs normalized fields returned by the existing authenticated read-only `get_positions()` path while `VALIDATION_LOCK` is active:

- symbol / side / contracts
- entry / mark / liquidation price
- reported real leverage
- unrealised PnL
- position margin
- exchange-confirmed stop loss / take profit
- calculated directional distance to liquidation

The wrapper returns the original position list unchanged and adds no order, cancel, leverage, stop, close, or other exchange mutation.

Runtime evidence must retain `execution_effect=NONE`.
