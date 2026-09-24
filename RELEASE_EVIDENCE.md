# Stage-C LIVE release evidence contract

**Status:** the contract, parser and a reference verifier are implemented (`bot/live_release_evidence.py`) and tested (`tests/test_live_release_evidence.py`). **No trusted production provider, protected release environment or production evidence exists.** Every live verdict is therefore `BLOCK`. Nothing in this document deploys, changes Railway or sends orders.

## 1. What the policy line proves, and what it does not
At startup the bot logs one sanitized line:

```
[LIVE_POLICY_OBSERVATION_V2] format=2 candidate_sha=<40-hex> deployment_id=<id>
  service_id_hash=<sha256> environment_id_hash=<sha256> generated_at=<UTC>
  policy_sha256=<hex> LEVERAGE=50 MAX_RISK_PCT=0.01 …(30 whitelisted keys)
```

- `candidate_sha` and `deployment_id` come from `RAILWAY_GIT_COMMIT_SHA` and `RAILWAY_DEPLOYMENT_ID`. The service and environment IDs are logged only as sha256 hashes. No other environment variable is read, and no secret can appear.
- `policy_sha256` is the sha256 of a **plaintext** payload. It proves **integrity only**. A match is `POLICY_CONTENT_MATCH` with `provenance = UNAUTHENTICATED`.
- A line supplied to the replay is only a research/test fixture. No LIVE evidence is read from the repository.

## 2. The envelope: `BGX_LIVE_RELEASE_EVIDENCE_V1`
```json
{
  "schema": "BGX_LIVE_RELEASE_EVIDENCE_V1", "evidence_version": 1,
  "candidate_sha": "<40-hex>", "generated_at_utc": "YYYY-MM-DDTHH:MM:SSZ",
  "research_artifact": {
    "oos_run_id": "…", "workflow_name": "NEXUS Real OOS Replay",
    "artifact_name": "nexus-oos-real-replay", "artifact_sha256": "<sha256 of nexus_oos_real_replay.json bytes>",
    "candidate_sha": "<40-hex>", "completed_at_utc": "…"
  },
  "railway": {"deployment_id": "…", "service_id": "…", "environment_id": "…",
              "deployment_commit_sha": "<40-hex>", "deployment_status": "SUCCESS"},
  "runtime_policy": {"candidate_sha": "<40-hex>", "policy_sha256": "<64-hex>", "observed_at_utc": "…"},
  "ci": {"exact_sha": "<40-hex>", "result": "PASS",
         "required_run_ids": {"Quality Check": "…", "Supply Chain Security": "…",
                              "Runtime Truth Candidate": "…", "NEXUS Real OOS Replay": "<= oos_run_id>"}},
  "protection": {
    "evidence_id": "…", "candidate_sha": "<40-hex>", "result": "PASS", "generated_at_utc": "…",
    "mode": "PER_JOB",
    "checks": {"prelive_protection_failclosed": {"check": "prelive_protection_failclosed",
                                                 "run_id": "…", "job_id": "…", "result": "PASS"}, "…": {}},
    "evidence_sha256": "<sha256 of the canonical protection object without this field>"
  },
  "human_approval": {
    "approval_reference": "…", "kind": "GITHUB_PROTECTED_ENVIRONMENT_APPROVAL | RELEASE_RECORD",
    "candidate_sha": "<40-hex>", "deployment_id": "…", "research_artifact_sha256": "<64-hex>",
    "release_evidence_digest": "<sha256 of the canonical envelope without human_approval>",
    "approver_reference": "…", "approved_at_utc": "…"
  }
}
```
Protection may instead use `"mode": "CERTIFICATE"` with `"certificate": {"run_id", "artifact_name": "protection-evidence", "artifact_sha256"}`. The certificate is a `PROTECTION_EVIDENCE_V1` artifact produced by the workflow `BGX Protection Evidence` on the exact SHA.

The envelope is a **claim**. No field is trusted because it appears in a JSON file.

## 3. What the verifier re-reads, and from where
| Component | Re-read from the trusted provider | Required |
|---|---|---|
| `RESEARCH_ARTIFACT_AUTHENTICATED` | The OOS run metadata **and** its uploaded `nexus_oos_real_replay.json` bytes (unzipped by the provider) | Workflow `NEXUS Real OOS Replay`; `head_sha` = candidate; `conclusion = success` (the strict research gate is the final step, so a run whose replay succeeded but whose gate failed does not count); artifact name `nexus-oos-real-replay`; sha256(bytes) = provider digest = envelope digest; artifact `candidate_sha` = candidate |
| `RESEARCH_PROMOTION` | — | **Recomputed** by running the research gate on the **trusted** artifact. A caller-supplied local artifact is used for comparison only (`LOCAL_ARTIFACT_DIFFERS_FROM_TRUSTED`). The envelope cannot claim this result |
| `POLICY_CONTENT_MATCH` | The runtime line | `policy_sha256` = the trusted artifact's `replay_policy_sha` |
| `LIVE_PROVENANCE_AUTHENTICATED` | Railway deployment metadata and **that deployment's** logs | Every `railway` field; `status = SUCCESS`; pinned service and environment; the line carries that deployment's ID and pinned hashes; not older than the deployment; ≤ 6 h old |
| `EXACT_DEPLOYMENT_SHA_MATCH` | Railway and the runtime line | trusted artifact SHA = envelope SHA = deployment `commitHash` = runtime `candidate_sha` |
| `CI_EVIDENCE` | Each workflow run | Exact `head_sha`, expected name, `conclusion = success`, ≤ 7 days. `NEXUS Real OOS Replay` must be the same run as the research artifact |
| `PROTECTION_EVIDENCE` | PER_JOB: each **job** (id, name, run, head SHA, conclusion) and its run. CERTIFICATE: the named workflow's run and its certificate artifact | PER_JOB: job name = check name; job belongs to the stated run; one job per check (one generic run can never satisfy several checks). CERTIFICATE: digest matches, candidate SHA matches, and every required check is present and `PASS` |
| `HUMAN_APPROVAL_EVIDENCE` | The provider-native approval record | `state = APPROVED`; same candidate SHA, deployment ID (or a record that explicitly lists this deployment in `covered_deployment_ids`), research-artifact digest and release-evidence digest; approved **after all automated Stage-C evidence completed, including protection evidence** (approval is the final release authorization, not a pre-review); ≤ 24 h old |
| `RELEASE_VERIFIER_TRUSTED` | The protected environment's verifier identity | The verifier SHA equals the SHA pinned by the protected environment and differs from the candidate SHA. The result reports `release_verifier_version` and `release_verifier_sha` |

## 4. Stale-evidence limits (predeclared, fixed before any Stage-C evidence exists)
| Evidence | Maximum age |
|---|---|
| Runtime policy observation | 6 h, and never before the deployment's `created_at` |
| Human approval | 24 h, and never before the completed pre-live evidence |
| CI runs, OOS run, protection evidence | 7 days |
| Future timestamps | at most 5 min of clock skew |

## 5. Stages
| Stage | Verdict | Meaning |
|---|---|---|
| A | `RESEARCH_PROMOTION` | Research gate. In Stage C it is recomputed on the trusted artifact. Independent of LIVE provenance |
| B | `PRELIVE_EVIDENCE` | Research artifact authenticated + policy content match + provenance + exact deployment SHA + CI + trusted verifier |
| C | `LIVE_RELEASE_PRECONDITIONS` | A + B + protection + human approval |
| — | `REAL_ORDER_ENABLEMENT = HUMAN_ACTION_REQUIRED` | Always. `authorizes_real_trading` is always false |

`production_ready = true` only when Stage C passes on source-authenticated evidence. The CLI configures no trusted provider, so it can never produce it.

## 6. Residual dependence inside the trusted artifact
The artifact carries compact per-block series (`block_ids`, `means`, `counts`). Once the whole artifact is authenticated by its CI digest, the gate re-validates each series and recomputes the **calendar** ACF:
- the three arrays have equal length;
- block IDs are integers, strictly increasing and unique;
- counts are positive integers;
- the series length equals `resampling_blocks`.

Blocks are paired only when their IDs differ by exactly k. For uplift, the series is `PAIRED_BLOCK_DELTA` (`a_sum`, `a_count`, `b_sum`, `b_count` per block); the gate rebuilds the delta from these aggregates and rejects any approved-only series. A zero-variance series is NOT_ESTIMABLE. Fewer than 20 calendar-valid pairs at any lag gives INSUFFICIENT_EVIDENCE. Nested fields are not authenticated separately: the artifact digest covers them.

## 7. Trust boundary: the candidate is not the root of trust
The software being released must not decide whether it may be released. `bot/live_release_evidence.py` in this repository is a **reference implementation**. The authoritative Stage-C verifier comes from a protected location, **never from the candidate branch or SHA**:

- **Option A (preferred):** a reusable GitHub Actions release workflow stored on protected `main` and referenced by an immutable commit SHA, running in a protected GitHub environment with required reviewers.
- **Option B:** a separate protected release-tools repository.
- **Option C:** a versioned release-verifier package whose digest is pinned by the protected release environment.

The protected release runner, and nothing the candidate controls, owns:
- the Railway read-only credentials;
- the GitHub read-only evidence credentials;
- access to the approval provider;
- the pinned production service and environment IDs;
- the pinned release-verifier version and SHA.

Provider trust comes from that protected execution environment and its credentials, **not from the `source_kind` field**, which any object can claim. `source_kind` is only an interface marker. Unit tests use in-memory fakes, and candidate-controlled code must never inject a production provider. A PR that weakens the verification rules cannot use those weakened rules to authorize itself, because the rules that run are the pinned ones.

## 8. Remaining work before Stage C can pass
1. The protected release environment and workflow (Option A), pinned by SHA and holding the credentials above.
2. Read-only providers:
   - Railway: deployment by ID and that deployment's logs;
   - GitHub: runs, jobs and artifacts;
   - approval: the protected-environment approval record.
3. The `BGX Protection Evidence` workflow, or per-check named jobs, on the exact SHA.
4. Pinned production service and environment IDs.

Until these exist: `LIVE_RELEASE_EVIDENCE = BLOCK`.
