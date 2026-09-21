# Production promotion path

BGX-REL-AUTO-DEPLOY-001 defines the production sequence:

```text
MAIN MERGE
  -> Quality Check
  -> Supply Chain Security
  -> Publish CI Attestation (exact SHA)
  -> Release Gate (exact SHA)
  -> Railway Wait for CI
  -> Railway build/deploy
  -> bot.ci_deploy_gate pre-deploy
  -> /ready healthcheck
```

## Required Railway setting

For the production `nexus7-bot` service, **Wait for CI must be enabled**.
The Railway service configuration exposes this as the GitHub source
`checkSuites=true` behavior.

This setting is an outer gate. It does not replace `python -m bot.ci_deploy_gate`,
which remains the in-deployment exact-SHA defense.

If Wait for CI is disabled, this release flow is not considered closed.
