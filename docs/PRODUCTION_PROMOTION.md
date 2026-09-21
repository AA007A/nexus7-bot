# Production promotion control

## Invariant

`main` is an integration branch, not a Railway deployment trigger. Production
may consume only an exact commit that was manually promoted after Quality and
Supply Chain Security produced the repository's exact-SHA attestation.

The intended sequence is:

1. merge to `main`;
2. Quality Check succeeds for the exact SHA;
3. Supply Chain Security succeeds for the same SHA;
4. Publish CI Attestation writes `passed/<sha>.txt` on `ci-attestations`;
5. an operator dispatches `Promote Production` with that full SHA;
6. the workflow verifies the SHA is the current `main`, verifies the exact
   attestation, and fast-forwards `production` without force;
7. `Production Release Gate` independently verifies main ancestry and the
   exact attestation on the `production` push;
8. Railway, connected to `production` with Wait for CI enabled, may start its
   deployment only after that gate succeeds;
9. Railway still runs `python -m bot.ci_deploy_gate` before deployment.

There is deliberately no `workflow_run` or push-triggered automatic promotion.
Each production candidate requires an explicit operator dispatch.

## Railway one-time configuration

For `BGX CAPITAL / production / nexus7-bot`:

- deployment branch: `production`;
- Wait for CI: enabled (`checkSuites=true`);
- pre-deploy command: keep `python -m bot.ci_deploy_gate`;
- health check: keep `/ready`;
- do not trigger Deploy Latest Commit while changing these settings.

The branch and Wait for CI settings live in Railway, not in `railway.toml`.
Their activation must be captured through a post-change service-config readback.

## Promotion procedure

1. Confirm the candidate is the current `main` SHA.
2. Confirm Quality, Security and Publish CI Attestation succeeded for it.
3. Open GitHub Actions, select `Promote Production`, choose `Run workflow`, and
   enter the full 40-character SHA.
4. Confirm `Production Release Gate` succeeds for that SHA.
5. Confirm Railway shows the same SHA and did not build or deploy before the
   gate completed.
6. Validate `/ready` and the debt-specific production evidence before closing
   the release record.

## Failure semantics

- stale or abbreviated SHA: promotion fails;
- SHA different from current `main`: promotion fails;
- absent or mismatched attestation: promotion fails;
- non-fast-forward production update: promotion fails;
- failed production release gate: Railway skips/blocks the deployment when Wait
  for CI is enabled;
- missing/unavailable attestation inside Railway: `ci_deploy_gate` remains
  fail-closed.

This control changes release orchestration only. It does not change ownership,
strategy, NEXUS, sizing, leverage, margin mode, SL/TP, execution policy, database
selection, or runtime financial behavior.

