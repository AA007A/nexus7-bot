# LIVE risk override

`LIVE_RISK_OVERRIDE_APPROVED` is an explicit operator acknowledgement for the controlled LIVE pilot.

Default behavior is fail-closed for account drawdown breaches: new entries remain blocked by the drawdown gate.

To deliberately allow new entries despite the configured account drawdown threshold, the variable must exactly equal the acknowledgement token implemented in `bot/operator_runtime_policy.py`.

This override does **not** bypass balance confirmation, maximum-position checks, duplicate-order protection, exchange/local reconciliation, instrument validation, protective-order requirements, or other execution-safety gates.

The operator-requested LIVE geometry remains unchanged by this feature: the margin target remains 50% of freshly authenticated available collateral and leverage continues to come from the existing `LEVERAGE` configuration.

Every exercised drawdown override is emitted at CRITICAL severity so it can be audited in runtime logs.
