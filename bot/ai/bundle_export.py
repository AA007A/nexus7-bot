"""Materialize and verify the deployable AI SHADOW observer bundle.

    python -m bot.ai.bundle_export --artifact artifacts/nexus_oos_real_replay.json \\
        --out artifacts/ai_shadow_observer_bundle --candidate-sha <sha> \\
        --deploy-manifest artifacts/shadow_observer_deploy_manifest/shadow_observer_deploy_manifest.json
    python -m bot.ai.bundle_export --verify artifacts/ai_shadow_observer_bundle

The bundle directory contains EXACTLY manifest.json, classifier.json,
regressor.json and bundle_metadata.json (no secrets). ``--verify`` reloads it
through the real runtime path (bot.ai.runtime.load_bundle: bundle hash, pin,
component hashes, policy hash, feature schema, AI version, hook population)
and runs one deterministic inference twice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

BUNDLE_FILES = ("manifest.json", "classifier.json", "regressor.json", "bundle_metadata.json")
METADATA_SCHEMA = "AI_SHADOW_OBSERVER_BUNDLE_METADATA_V1"
DEPLOY_SCHEMA = "AI_SHADOW_OBSERVER_DEPLOY_MANIFEST_V1"


class BundleExportError(RuntimeError):
    pass


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2) + "\n"


def select_bundle(artifact: dict) -> dict:
    """SHADOW_CHALLENGER when the research gate passed, else the zero-order
    SHADOW_OBSERVER. Never a PAPER/LIVE state."""
    ai = ((artifact or {}).get("candidate_research") or {}).get("ai_meta_model") or {}
    for key in ("shadow_challenger", "shadow_observer"):
        b = ai.get(key) or {}
        if b.get("created") and b.get("bundle"):
            if b["bundle"].get("lifecycle_state") not in ("SHADOW_CHALLENGER", "SHADOW_OBSERVER"):
                raise BundleExportError("only SHADOW_* bundles may be exported")
            return b
    raise BundleExportError("no SHADOW bundle in the replay artifact")


SHADOW_LIFECYCLES = ("SHADOW_OBSERVER", "SHADOW_CHALLENGER")
SHA40 = re.compile(r"[0-9a-f]{40}")
# metadata key -> ModelBundle manifest key (exact agreement required at observer startup)
METADATA_BINDING = {"bundle_sha256": "bundle_sha256", "policy_sha256": "decision_policy_sha256",
                    "feature_schema_sha256": "feature_schema_sha256", "hook_population": "hook_population",
                    "hook_profile": "hook_profile", "lifecycle_state": "lifecycle_state",
                    "dataset_manifest_sha256": "training_dataset_manifest_sha256"}


def bundle_metadata(man: dict, *, candidate_sha: str | None, claims: dict | None = None,
                    training_dataset_rows=None) -> dict:
    return {"schema": METADATA_SCHEMA, "candidate_code_sha": candidate_sha,
            "bundle_sha256": man["bundle_sha256"], "policy_sha256": man["decision_policy_sha256"],
            "feature_schema_sha256": man["feature_schema_sha256"], "hook_population": man.get("hook_population"),
            "hook_profile": man.get("hook_profile"), "lifecycle_state": man["lifecycle_state"],
            "dataset_manifest_sha256": man.get("training_dataset_manifest_sha256"),
            "training_code_sha": man.get("training_code_sha"),
            "training_dataset_rows": training_dataset_rows, "created_at": man["created_at"],
            "claims": claims or {"edge_claim": False, "order_authority": False, "live_authority": False},
            "secrets_included": False}


def verify_metadata(meta, manifest: dict) -> dict:
    """bundle_metadata.json must agree EXACTLY with the loaded ModelBundle
    manifest; unknown or malformed metadata is refused."""
    if not isinstance(meta, dict) or meta.get("schema") != METADATA_SCHEMA:
        raise BundleExportError("bundle metadata schema unknown or malformed")
    for mk, bk in METADATA_BINDING.items():
        if meta.get(mk) is None or meta.get(mk) != manifest.get(bk):
            raise BundleExportError(f"bundle metadata {mk} differs from the loaded bundle")
    if not SHA40.fullmatch(str(meta.get("candidate_code_sha") or "")):
        raise BundleExportError("bundle metadata candidate_code_sha is not an exact 40-hex SHA")
    if meta.get("lifecycle_state") not in SHADOW_LIFECYCLES:
        raise BundleExportError(f"lifecycle {meta.get('lifecycle_state')} is not SHADOW-safe")
    return meta


def export(artifact: dict, out_dir, *, candidate_sha: str | None) -> dict:
    b = select_bundle(artifact)
    man = b["bundle"]
    ds = (((artifact.get("candidate_research") or {}).get("ai_meta_model") or {}).get("dataset_manifest") or {})
    cand = candidate_sha or artifact.get("candidate_sha")
    if man.get("training_code_sha") != cand:
        raise BundleExportError("CODE_BUNDLE_SHA_MISMATCH: bundle training_code_sha != candidate sha")
    meta = bundle_metadata(man, candidate_sha=cand, claims=b.get("claims"),
                           training_dataset_rows=ds.get("training_dataset_rows"))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {"manifest.json": man, "classifier.json": b["classifier_artifact"],
             "regressor.json": b["regressor_artifact"], "bundle_metadata.json": meta}
    digests = {}
    for name, obj in files.items():
        text = _dump(obj)
        (out / name).write_text(text, encoding="utf-8")
        digests[name] = hashlib.sha256(text.encode()).hexdigest()
    return {"bundle_sha256": man["bundle_sha256"], "file_sha256": digests,
            "bundle_digest": hashlib.sha256(_dump(digests).encode()).hexdigest(), "metadata": meta}


def verify(out_dir, *, pinned_bundle_sha: str | None = None) -> dict:
    """Load through the REAL runtime path and run one deterministic inference."""
    import numpy as np
    from bot.ai import features as fx
    from bot.ai import hook as ai_hook
    from bot.ai import runtime as rt
    out = Path(out_dir)
    present = sorted(p.name for p in out.iterdir() if p.is_file())
    if present != sorted(BUNDLE_FILES):
        raise BundleExportError(f"bundle files must be exactly {sorted(BUNDLE_FILES)}; got {present}")
    meta = json.loads((out / "bundle_metadata.json").read_text())
    pin = pinned_bundle_sha or meta["bundle_sha256"]
    bundle = rt.load_bundle({"AI_BUNDLE_DIR": str(out), "AI_BUNDLE_SHA256": pin})
    if bundle.sha256 != meta["bundle_sha256"]:
        raise BundleExportError("metadata bundle sha differs from the manifest")
    verify_metadata(meta, bundle.manifest)
    if bundle.manifest.get("training_code_sha") != meta["candidate_code_sha"]:
        raise BundleExportError("CODE_BUNDLE_SHA_MISMATCH")
    if bundle.manifest.get("hook_profile") != ai_hook.TRAINING_PROFILE or \
            bundle.manifest.get("hook_population") != ai_hook.POPULATION:
        raise BundleExportError("hook population/profile mismatch")
    X = np.zeros((1, len(fx.MODEL_FEATURES)))
    runs = [(float(bundle.calibrator.transform(bundle.classifier.predict_proba(X))[0]),
             float(bundle.regressor.predict(X)[0])) for _ in range(2)]
    if runs[0] != runs[1]:
        raise BundleExportError("non-deterministic inference")
    return {"verified": True, "bundle_sha256": bundle.sha256, "policy_sha256": bundle.policy.sha256,
            "candidate_code_sha": meta["candidate_code_sha"],
            "training_code_sha": bundle.manifest.get("training_code_sha"),
            "dataset_manifest_sha256": bundle.manifest.get("training_dataset_manifest_sha256"),
            "metadata_verified": True,
            "hook_profile": bundle.manifest["hook_profile"], "lifecycle_state": bundle.manifest["lifecycle_state"],
            "deterministic_inference": {"p_calibrated_zero_input": runs[0][0], "predicted_gross_r_zero_input": runs[0][1]}}


def deploy_manifest(export_result: dict, *, repo_root=".") -> dict:
    """Non-secret deployment manifest for the isolated observer service."""
    from bot.ai import forward_evidence as fe
    from bot.ai import shadow_observer as so
    root = Path(repo_root)

    def sha(p):
        f = root / p
        return hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
    from bot import nexus_oos_replay_manifest as rm
    m = export_result["metadata"]
    replay_sha = rm.load().sha256
    prov = {"candidate_code_sha": m["candidate_code_sha"], "training_code_sha": m.get("training_code_sha"),
            "bundle_sha256": m["bundle_sha256"], "dataset_manifest_sha256": m["dataset_manifest_sha256"],
            "replay_policy_manifest_sha256": replay_sha,
            "forward_contract_sha256": fe.CONTRACT_SHA256["SHADOW"]}
    prov["mutually_consistent"] = bool(
        SHA40.fullmatch(str(prov["candidate_code_sha"] or "")) and
        prov["candidate_code_sha"] == prov["training_code_sha"] and all(v for v in prov.values()))
    return {"schema": DEPLOY_SCHEMA, "service": "ai-shadow-observer (isolated; NOT the production service)",
            "start_command": "python -m bot.ai.shadow_observer",
            "railway_config": "deploy/ai_shadow_observer/railway.toml",
            "railway_config_sha256": sha("deploy/ai_shadow_observer/railway.toml"),
            "service_doc_sha256": sha("deploy/ai_shadow_observer/SERVICE.md"),
            "candidate_code_sha": m["candidate_code_sha"], "bundle_sha256": m["bundle_sha256"],
            "policy_sha256": m["policy_sha256"], "feature_schema_sha256": m["feature_schema_sha256"],
            "hook_population": m["hook_population"], "hook_profile": m["hook_profile"],
            "lifecycle_state": m["lifecycle_state"], "bundle_file_sha256": export_result["file_sha256"],
            "bundle_digest": export_result["bundle_digest"],
            "forward_contract": fe.SHADOW_CONTRACT["name"], "forward_contract_sha256": fe.CONTRACT_SHA256["SHADOW"],
            "replay_policy_manifest_sha256": replay_sha,
            "training_code_sha": m.get("training_code_sha"),
            "dataset_manifest_sha256": m["dataset_manifest_sha256"],
            "provenance": prov,
            "pinned_env": {"SHADOW_REPLAY_POLICY_SHA256": replay_sha,
                           "SHADOW_FORWARD_CONTRACT_SHA256": fe.CONTRACT_SHA256["SHADOW"],
                           "AI_BUNDLE_SHA256": m["bundle_sha256"]},
            "code_sha_authority": "RAILWAY_GIT_COMMIT_SHA (Railway-provided; must equal candidate_code_sha); "
                                  "CANDIDATE_SHA only offline and must agree when both are set",
            "symbol_universe": list(fe.SHADOW_UNIVERSE),
            "symbol_universe_sha256": fe.universe_sha256(fe.SHADOW_UNIVERSE),
            "forward_window": {"duration_days": fe.WINDOW_DAYS, "expected_boundaries": fe.EXPECTED_BOUNDARIES,
                               "start": "first 15m boundary after successful startup (derived, not configured)",
                               "optional_env": "AI_EVIDENCE_NEW_WINDOW_AFTER=<latest window_id> (single-use; "
                                               "only for a new window after the previous one ended)"},
            "superseded_contracts": fe.SUPERSEDED,
            "forward_artifact_export": "python -m bot.ai.forward_artifact --from-evidence-db --output PATH",
            "required_env_names": ["AI_EXECUTION_MODE=SHADOW", "AI_BUNDLE_DIR", "AI_BUNDLE_SHA256", "SHADOW_SYMBOLS",
                                   "SHADOW_REPLAY_POLICY_SHA256", "SHADOW_FORWARD_CONTRACT_SHA256",
                                   "RAILWAY_GIT_COMMIT_SHA (Railway-provided)",
                                   "EVIDENCE_DATABASE_URL (secret, dedicated PostgreSQL)", "EVIDENCE_DB_AUTHORITY_ID",
                                   "PRODUCTION_DB_AUTHORITY_ID", "PRODUCTION_DB_FINGERPRINT"],
            "forbidden_env_names": list(so.FORBIDDEN_CREDENTIALS),
            "zero_order": {"exchange_credentials": False, "mutating_client_methods": False,
                           "execution_lease": False, "orders_sent": 0},
            "secrets_included": False, "deployed": False}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact")
    ap.add_argument("--out")
    ap.add_argument("--candidate-sha")
    ap.add_argument("--deploy-manifest")
    ap.add_argument("--verify")
    a = ap.parse_args(argv)
    if a.verify:
        print(json.dumps(verify(a.verify), indent=2, sort_keys=True))
        return 0
    art = json.loads(Path(a.artifact).read_text())
    try:
        res = export(art, a.out, candidate_sha=a.candidate_sha)
    except BundleExportError as exc:
        print(json.dumps({"exported": False, "reason": str(exc)}))
        return 2
    if a.deploy_manifest:
        p = Path(a.deploy_manifest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_dump(deploy_manifest(res)), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "metadata"} | {"metadata": res["metadata"]},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
