# Railway production promotion contract

## Invariant

Normal pushes and merges to `main` MUST NOT trigger a `nexus7-bot`
production deployment.

The Railway service watches exactly one path:

```
/.railway/promote.txt
```

That marker is intentionally absent from normal hardening PRs. A production
promotion is a separate, explicit action.

## Promotion sequence

1. Merge code to `main`.
2. Wait for **Quality Check** and **Supply Chain Security** on the exact main SHA.
3. Wait for **Publish CI Attestation** to publish
   `ci-attestations/passed/<sha>.txt`.
4. Manually run **Prepare Railway Promotion** with that exact SHA.
5. The workflow verifies:
   - the SHA is exactly the current `main` HEAD;
   - the exact Quality+Security attestation exists.
6. The workflow opens a PR that changes only `.railway/promote.txt`.
7. Review and merge that promotion PR.
8. Railway sees the watched path change and starts the intentional deployment.
9. Railway still executes `python -m bot.ci_deploy_gate` as pre-deploy defense.

## Failure semantics

- A normal main merge: **no Railway deployment**.
- Missing/failed CI attestation: **no promotion PR**.
- Stale target SHA: **no promotion PR**.
- Promotion PR containing code changes: **must not be merged**.
- Missing exact attestation for the deployed promotion SHA:
  `ci_deploy_gate` remains fail-closed and blocks pre-deploy.

## Non-scope

This mechanism does not change strategy, NEXUS, sizing, leverage, CROSS,
SL/TP, order routing, execution ownership, database state or trading policy.
