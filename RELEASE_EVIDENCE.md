# Stage-C LIVE release evidence contract

**Status:** the contract, parser and verifier are implemented (`bot/live_release_evidence.py`) and tested (`tests/test_live_release_evidence.py`). **No trusted production source is implemented, and no production evidence exists.** Every live verdict is therefore `BLOCK`. Nothing in this document deploys, changes Railway or sends orders.

## 1. What the policy line proves, and what it does not
At startup the bot logs one sanitized line:

```
[LIVE_POLICY_OBSERVATION_V2] format=2 candidate_sha=<40-hex> deployment_id=<id>
  service_id_hash=<sha256> environment_id_hash=<sha256> generated_at=<UTC>
  policy_sha256=<hex> LEVERAGE=50 MAX_RISK_PCT=0.01 …(30 whitelisted keys)
```

- `candidate_sha` and `deployment_id` come from the platform variables `RAILWAY_GIT_COMMIT_SHA` and `RAILWAY_DEPLOYMENT_ID`. The service and environment IDs are logged only as sha256 hashes.
- No other environment variable is read. API keys, exchange secrets, database URLs, tokens and passwords cannot appear in the line.
- `policy_sha256` is the sha256 of the **plaintext** payload. It proves **integrity only**. Anyone who knows the policy values can build the same line.
- A matching line is therefore reported as `POLICY_CONTENT_MATCH` with `provenance = UNAUTHENTICATED`, never as an authenticated LIVE attestation.
- The replay accepts such a line only as a research/test fixture (`--policy-observation-fixture`). The PR workflow never passes one, and no LIVE evidence is read from the repository.

## 2. The envelope: `BGX_LIVE_RELEASE_EVIDENCE_V1`
```json
{
  "schema": "BGX_LIVE_RELEASE_EVIDENCE_V1",
  "evidence_version": 1,
  "candidate_sha": "<40-hex>",
  "generated_at_utc": "YYYY-MM-DDTHH:MM:SSZ",
  "railway": {
    "deployment_id": "…", "service_id": "…", "environment_id": "…",
    "deployment_commit_sha": "<40-hex>", "deployment_status": "SUCCESS"
  },
  "runtime_policy": {
    "candidate_sha": "<40-hex>", "policy_sha256": "<64-hex>",
    "observed_at_utc": "YYYY-MM-DDTHH:MM:SSZ"
  },
  "ci": {
    "exact_sha": "<40-hex>",
    "required_run_ids": {"Quality Check": "…", "Supply Chain Security": "…",
                         "Runtime Truth Candidate": "…"},
    "result": "PASS"
  },
  "protection": {
    "evidence_id": "…", "candidate_sha": "<40-hex>", "result": "PASS",
    "generated_at_utc": "…",
    "checks": {"release_proof": {"run_id": "…", "result": "PASS"},
               "protection_readiness_authority": {"run_id": "…", "result": "PASS"},
               "prelive_protection_failclosed": {"run_id": "…", "result": "PASS"},
               "durable_execution_restart": {"run_id": "…", "result": "PASS"},
               "kucoin_native_tpsl": {"run_id": "…", "result": "PASS"}},
    "evidence_sha256": "<sha256 of the canonical protection object without this field>"
  },
  "human_approval": {
    "approval_reference": "…", "kind": "GITHUB_PROTECTED_ENVIRONMENT_APPROVAL | RELEASE_RECORD",
    "candidate_sha": "<40-hex>", "approver_reference": "…", "approved_at_utc": "…"
  }
}
```

The envelope is a **claim**. The verifier trusts no field just because it appears in a JSON file.

## 3. Trusted sources (read-only)
`TrustedSources` is a provider interface with read methods only: `pinned_production`, `railway_deployment`, `railway_deployment_logs`, `ci_run` and `approval`. The verifier calls nothing else, and a test enforces this. A provider must declare `source_kind = READ_ONLY_CONTROL_PLANE`.

| Claim | Re-read from | Must equal |
|---|---|---|
| `railway.*` | Railway control-plane deployment metadata | Every field. `status == SUCCESS`. Service and environment equal the **pinned** production identifiers |
| `runtime_policy` | The `LIVE_POLICY_OBSERVATION_V2` line in **that deployment's** logs, with that deployment's ID | `policy_sha256`, `observed_at`. Service/environment hashes equal the pinned IDs' hashes. `policy_sha256` equals the replay manifest digest |
| Exact deployment SHA | Control plane and runtime line | artifact `candidate_sha` = envelope `candidate_sha` = deployment `commitHash` = runtime `candidate_sha` |
| `ci.required_run_ids` | CI provider | Each run: exact `head_sha`, expected workflow name, `conclusion = success` |
| `protection` | CI provider for every check's `run_id` | Digest matches. `candidate_sha` matches. Every required check is PASS on an exact-SHA successful run |
| `human_approval` | Approval provider (protected-environment approval or release record) | Every field. `state = APPROVED`. Same `candidate_sha` |

A caller-supplied `--candidate-sha` cannot establish identity; the CLI no longer accepts it. `--protection-readiness PASS` and free-text human authorization have been removed. A bare `"PASS"` or a free-text approval makes the envelope malformed.

## 4. Stale-evidence limits (predeclared, fixed before any Stage-C evidence exists)
| Evidence | Maximum age at verification |
|---|---|
| Runtime policy observation | 6 h, and never earlier than the deployment's `created_at` |
| Human approval | 24 h |
| CI runs / protection evidence | 7 days |
| Future timestamps | at most 5 min of clock skew |

Evidence is also rejected when:
- the candidate SHA, deployment ID, service or environment differs;
- the runtime line comes from another deployment;
- the line is missing from the deployment's logs.

## 5. Stages
| Stage | Verdict | Meaning |
|---|---|---|
| A | `RESEARCH_PROMOTION = PASS/BLOCK` | Research gate. Independent of LIVE provenance. `LIVE_POLICY_OBSERVATION_PENDING` allowed. Never production-ready |
| B | `PRELIVE_EVIDENCE = PASS/BLOCK` | Policy content match + authenticated provenance + exact deployment SHA + exact-SHA CI |
| C | `LIVE_RELEASE_PRECONDITIONS = PASS/BLOCK` | A + B + protection evidence + human approval + full context parity |
| — | `REAL_ORDER_ENABLEMENT = HUMAN_ACTION_REQUIRED` | Always. `authorizes_real_trading` is always false |

`production_ready = true` is emitted only by the live gate, and only when every precondition passed on **source-authenticated** evidence. The CLI configures no trusted source, so it can never emit it.

## 6. Remaining work before Stage C can pass
1. A read-only Railway provider: a scoped read token held by a protected release environment, never by the repo or the PR workflow. It must fetch the deployment by ID and that deployment's logs.
2. A CI provider (GitHub API, read-only) and an approval provider (protected-environment approval record).
3. Pinned production service and environment IDs, held in the release environment's reviewed configuration.
4. A release workflow that runs the verifier and publishes the verdict as an artifact. It must not deploy anything.

Until these exist: `LIVE_RELEASE_EVIDENCE = BLOCK`.
