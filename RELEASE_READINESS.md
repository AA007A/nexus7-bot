# RELEASE_READINESS

- PR: #415 (draft; **do not merge**). Branch: `claude/nexus7-production-hardening-5aq2sc`.
- Starting SHA of the replay-parity phase: `70b00b4fe403aa2fad7ace01a951555f8e731c34`. Earlier phase start: `a1a5ea0`. Original baseline and current production: `77ae453e3d01dc269c3b1ee3a4225900c22d9634`.
- Deployments: **none**. Railway variables read or changed: **none**. Exchange orders: **none**. Merges: **none**.

## Exact-SHA CI record

| SHA | Quality | Security | Runtime Truth | Diagnostics | Real OOS Replay |
|---|---|---|---|---|---|
| `b777cbb` (90 d, IID era) | 35946377950 ✅ | 35946378044 ✅ | 35946378031 ✅ | 35946378034 ✅ | 35946377972 ❌ strict gate (evidence), replay OK |
| `2932572` (180 d, dependence-aware + portfolio) | 35951346252 ✅ | 35951346254 ✅ | 35951346256 ✅ | 35951346297 ✅ | 35951346282 ❌ strict gate (evidence), replay OK |
| `a9a919a` (docs only on top of `2932572`) | 35951515753 ✅ | 35951515761 ✅ | 35951515772 ✅ | 35951515778 ✅ | 35951515777 (see final report) |

The final head (the docs commit after `a9a919a`) gets its own complete CI run. Its run IDs are recorded in the final report. No green check from an earlier SHA is cited as proof for a later one.

## Status by layer

| Layer | Status | Evidence |
|---|---|---|
| **ENGINEERING_SAFETY** | **Met (offline)** | Non-overridable drawdown gate; daily-stop override removed (P1-03); conservative daily-stop precision; stop-risk-authoritative sizing; strict CI promotion gate. 1,726/1,726 offline tests pass; `RELEASE_PROOF=PASS`; the runtime contract passes in the controlled-LIVE bootstrap. Not validated in PAPER or SHADOW. |
| **SIGNAL_EDGE** | **Failed: NEGATIVE EXPECTANCY** | NEXUS-approved −0.324 R per candidate, authority CI [−0.496, −0.175]; every symbol, month and regime negative. |
| **STATISTICAL_CONFIDENCE** | **Failed** | Uplift +0.051 R, authority CI [−0.043, +0.143]. IID looked significant; dependence-aware inference does not. Effective n about 1,283 over 171 unique days. |
| **PORTFOLIO_EDGE** | **Failed** | Portfolio replay −49.4%, max drawdown 50.3%, halted by the drawdown hard gate; −0.788 R per trade. |
| **CONTEXT_PARITY** | **Incomplete** | No historical OI or order book. Replayable optional context shows no measurable effect (ablation). |
| **REPLAY_PARITY** | **INCOMPLETE** | Production exits and pre-trade gates are traced and classified (OOS_REPORT §C). Exits are bar-approximated. Spread/depth, market-risk feeds, the pilot session cap and private CROSS MMR are not replayable. The gate blocks with EXIT/PRETRADE/PORTFOLIO_PARITY_INCOMPLETE. |
| **RELEASE_READINESS** | **Not ready** | See blockers. |

## Remaining blockers
1. **Negative net expectancy** in both the candidate and the portfolio replay, over 180 days and 12 symbols.
2. **The NEXUS edge is not statistically established.** Uplift over a losing baseline is not significant under block bootstrap, and it would not be edge even if it were.
3. **Portfolio replay loses 49%** and hits the 50% drawdown hard gate.
4. **No cost-stress survival.** Break-even requires about 86% lower costs.
5. **Historical context parity is incomplete** (OI, order book). The production gate is not relaxed for this.
6. **Replay parity is incomplete.** Exits are bar-approximated, and several pre-trade gates are not replayable (OOS_REPORT §C). The legacy exit-model bugs are fixed: shifted native stops, the 40-bar exit and the TP1 rule.
7. **Horizon.** 180 days (7 months) is covered; 365 days or more needs sharding. Multi-year durability: INSUFFICIENT EVIDENCE.
8. **The probability heuristic is miscalibrated**, and confidence is anti-predictive. It stays telemetry/veto only.
9. **Strategy-level gates are not ablated** (refactor required).
9b. **Production capacity facts.** At most one concurrent LIVE position, and 2 new orders per process session. Trailing after TP1 uses an un-rescaled peak (P1-17).
10. **PAPER and SHADOW validation** of this candidate have not been performed.
11. **Production state.** Live drawdown is 59.40%, above the 50% limit. Once deployed, this code blocks all new entries until an operator performs a reviewed HWM rebase or equity recovers. That is the intended outcome.
12. **Branch protection** (required checks) is a repository setting and cannot be verified from code.
13. **Human approval** has not been given.

## Verdict
The engineering and risk-control hardening is done and tested offline. The trading strategy has measured negative expectancy, and its NEXUS filter has no statistically established edge. Deploying it would expose capital to a strategy with negative expected value.

PRODUCTION_READY = NO
