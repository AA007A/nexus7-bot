# RELEASE_READINESS

- Candidate branch: `claude/nexus7-production-hardening-5aq2sc` (base `77ae453`)
- Deployment performed: **none**. Railway variables read or changed: **none**. Orders sent: **none**.

## Release process status

| # | Step | Status |
|---|---|---|
| 1 | Baseline audit | Done: AUDIT_10_10.md. Baseline suite 1607/1607 |
| 2 | Regression tests first | Done: 14 of the new P0 tests failed on baseline code (commit `7c1bca9`) |
| 3 | P0 risk fixes | Done: drawdown, sizing, loss budget, daily stop, min-lot, CROSS stress, config contract |
| 4 | Full offline suite | **1669/1669 pass** (`python -m tests.run_offline`) |
| 5 | Static analysis | compileall ok; ruff critical ok; pyflakes undefined names 0; selfcheck critical 0 |
| 6 | Release proof | `RELEASE_PROOF=PASS` |
| 7 | OOS real replay | **Not run on this branch** (sandbox network blocked). Latest CI evidence: `AI_EDGE_NOT_PROVEN`, negative net expectancy |
| 8 | Paired bootstrap robustness | Latest CI evidence: CI [−0.043, +0.418] R, not strictly positive |
| 9 | PAPER validation | Not performed |
| 10 | SHADOW validation | Not performed (offline bootstrap only) |
| 11 | Candidate vs production comparison | Sizing comparison is analytic only (RISK_INVARIANTS.md) |
| 12 | Release report | This document |
| 13 | Human approval | **Pending** |
| 14 | Controlled pilot | Not authorized |

## What deploying this branch would do in production

Production is at drawdown 59.40% against a 50% limit. With this branch, **no new positions will open**, whatever `LIVE_RISK_OVERRIDE_APPROVED` is set to. Existing positions stay managed, protected and reconciled. This is the intended fail-closed outcome. Entries resume only if equity recovers below the limit, or after an operator-reviewed, reconciled HWM rebase (`rebase_peak_equity`) or `MAX_DRAWDOWN` change. Either of those is a human decision and outside this change.

If `MAX_RISK_PCT` in Railway exceeds 0.02, or contradicts `DAILY_STOP_LOSS_PCT`/`MAX_DRAWDOWN`, new entries are blocked with `[RISK_POLICY_INVALID]`. Check the startup log line `[OPERATOR_RUNTIME_POLICY] ... policy_valid=` after any deploy.

## Remaining blockers

1. **Negative net expectancy.** The latest OOS replay shows −0.53 R per NEXUS-approved trade and −0.70 R baseline, net of costs. Capital should not be exposed to a strategy with negative measured expectancy.
2. **NEXUS edge not proven.** `AI_EDGE_NOT_PROVEN`: the uplift CI includes 0, and historical-context parity is incomplete (no historical OI or order book).
3. **OOS replay not re-run on this candidate SHA** (network-restricted sandbox). It must run on the PR.
4. **Daily-stop date-scoped override (P1-03).** `DAILY_STOP_OVERRIDE_UTC_DAY` can still re-enable new entries after a proven daily-stop breach. This violates the "override may only reduce risk" invariant.
5. **KuCoin CROSS risk-rate semantics not verified on a live account.** The stress formula and the default 0.50 threshold are conservative assumptions, not measured exchange behavior.
6. **Probability is an uncalibrated heuristic.** There is not enough data to calibrate. Downstream EV uses it.
7. **PAPER and SHADOW validation of this candidate not performed.**
8. **Regime/symbol segmentation and threshold sweep not available** (INSUFFICIENT EVIDENCE).
9. **Branch protection.** Whether `NEXUS Real OOS Replay` and `Quality Check` are *required* checks is a repository setting. It cannot be verified or changed from code.
10. **CoinGlass entitlement.** The 401 is now reported truthfully as FAILED with the Binance fallback active. Fixing the key/plan is an operator action.
11. Architecture debt: superseded inner sizing wrappers (P2-01), 4 REVIEW_MEDIUM silent handlers, and the 3,992-line `engine.py`. None of these blocks safety, but they raise review cost.

## Verdict

The risk-control P0s are fixed and tested. The system is **not** ready for capital, because blockers 1–7 are unresolved. Passing tests do not change that.

PRODUCTION_READY = NO
