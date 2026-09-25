"""Phase 8H artifacts + live PREFLIGHT (public KuCoin + disposable/research Postgres, PREFLIGHT_ONLY namespace)."""
from __future__ import annotations

import argparse
import asyncio
import json
import resource
import time
from pathlib import Path

from alpha_collector import collector as K
from alpha_collector import contract as C
from alpha_collector import db as D
from alpha_collector import sources as S

STORAGE_BUDGET_GB_PER_YEAR = 1000          # predeclared: no multi-TB/year design without explicit justification

ARCHITECTURE = {
    "service": "bgx-alpha-prospective-collector (BGX-RESEARCH only; NOT deployed by Phase 8H)",
    "package": "alpha_collector (imports nothing from the trading bot package `bot`)",
    "process": "single asyncio process: REST pollers (OI, funding/mark, server time), WS execution consumer, WS level2 "
               "consumer + in-memory books with REST resync, 5 s book feature sampler, trade buffer flush + 1m "
               "aggregation, heartbeat writer, daily sealer",
    "database": f"dedicated research Postgres, schema `{C.DB_SCHEMA}`, authority `{C.DB_AUTHORITY}`; env PROSPECTIVE_DB_URL "
                "(DATABASE_URL is forbidden to avoid inheriting a production link)",
    "modes": {"PREFLIGHT": "verify + short WS session + PREFLIGHT_ONLY writes, then idle (never collects)",
              "COLLECT": f"requires PROSPECTIVE_T0_AUTHORIZATION={K.T0_AUTH_PHRASE!r} and PROSPECTIVE_T0_MS"},
    "isolation": "no order/account endpoints (path allow-list), no credentials (env guard), crash-isolated from trading",
    "railway_config": "deploy/alpha_collector/railway.toml (not applied)"}


def static_artifacts(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)

    def dump(name, obj):
        (out / name).write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")
    dump("prospective_alpha_data_contract.json", {"contract": C.CONTRACT, "sha256": C.CONTRACT_SHA256})
    dump("collection_epoch_manifest.json", {"schema": C.EPOCH_SCHEMA, "schema_sha256": C.SHAS["COLLECTION_EPOCH_SCHEMA_SHA256"],
                                            "epoch_id": C.EPOCH_ID, "status": "NOT_STARTED (T0 not set)",
                                            "t0_rule": "set once by an explicitly authorized COLLECT start; never moves backward"})
    dump("source_endpoint_manifest.json", {"endpoints": C.SOURCE_ENDPOINTS, "sha256": C.SHAS["SOURCE_ENDPOINT_MANIFEST_SHA256"]})
    dump("symbol_mapping.json", {"mapping": C.SYMBOL_MAPPING, "sha256": C.SHAS["SYMBOL_MAPPING_SHA256"]})
    dump("canonical_schema.json", {"schema": C.CANONICAL_SCHEMA, "sha256": C.SHAS["CANONICAL_SCHEMA_SHA256"]})
    (out / "database_schema.sql").write_text(D.DDL + "\n")
    dump("collector_architecture.json", ARCHITECTURE)
    dump("sampling_policy.json", {"policy": C.SAMPLING_POLICY, "aggregation": C.AGGREGATION,
                                  "sha256": C.SHAS["SAMPLING_POLICY_SHA256"]})
    dump("retention_policy.json", {"policy": C.RETENTION_POLICY, "sha256": C.SHAS["RETENTION_POLICY_SHA256"]})
    dump("integrity_policy.json", {"policy": C.INTEGRITY_POLICY, "sha256": C.SHAS["INTEGRITY_POLICY_SHA256"]})
    dump("gap_detection_policy.json", C.GAP_POLICY)
    dump("book_reconstruction_policy.json", C.BOOK_POLICY)
    dump("source_health_contract.json", {"health": C.HEALTH_CONTRACT, "retry": C.RETRY_POLICY})
    return {**C.SHAS, "DATABASE_SCHEMA_SHA256": D.DB_SCHEMA_SHA256}


async def live_preflight(db_url: str, seconds: int) -> dict:
    t_start = time.time()
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    store = await D.PgStore.connect(db_url)
    await store.migrate()
    await store.init_authority()
    rest = S.RestClient()
    rep = {"started_ms": S.now_ms()}
    try:
        rep["clock"] = await S.server_skew(rest)
        col = K.Collector(store, rest, mode="PREFLIGHT", code_sha="preflight")
        st = await col.startup()
        rep["contract_multipliers"] = st["multipliers"]
        rep["mapping_ok"] = sorted(st["multipliers"]) == sorted(C.SYMBOLS)
        await col.poll_once()
        counts = {"trades": 0, "book": 0}
        on_t, on_b = col.on_trade, col.on_book

        async def ct(m, o):
            counts["trades"] += 1
            await on_t(m, o)

        async def cb(m, o):
            counts["book"] += 1
            await on_b(m, o)
        col.on_trade, col.on_book = ct, cb
        t0 = time.time()
        await col.run(stop_after_s=seconds)
        dur = time.time() - t0
        await col.poll_once()
        await col.heartbeat("PREFLIGHT_OK")
        rep["ws_seconds"] = round(dur, 1)
        rep["messages_per_sec"] = {k: round(v / dur, 2) for k, v in counts.items()}
        rep["book_updates_per_sec_per_symbol"] = round(counts["book"] / dur / len(C.SYMBOLS), 2)
        rep["trades_per_sec_all_symbols"] = round(counts["trades"] / dur, 3)
        rep["book_states_end"] = {s: b.state for s, b in col.books.items()}
        rep["book_stats"] = {s: b.stats for s, b in col.books.items()}
        rep["health"] = {k: h.c for k, h in col.health.items()}
        rep["persisted"] = col.persisted
        rep["gaps_recorded"] = col.gaps
        rows = {t: await store.count(t, include_preflight=True) for t in D.DATA_TABLES}
        rep["rows_preflight_only"] = rows
        rep["rows_prospective_epoch"] = {t: await store.count(t) for t in D.DATA_TABLES}
        sizes = {}
        for t in D.DATA_TABLES:
            sz = await store.c.fetchval(f"SELECT pg_total_relation_size('{C.DB_SCHEMA}.{t}')")
            sizes[t] = {"bytes": int(sz), "rows": rows[t], "bytes_per_row": (int(sz) / rows[t]) if rows[t] else None}
        rep["table_sizes"] = sizes
        # timestamp validation on persisted rows
        bad = 0
        total = 0
        for t in D.DATA_TABLES:
            for r in await store.rows(t):
                total += 1
                if "CLOCK_ORDER_VIOLATION" in r["quality_flags"] or r["event_ts"] > r["ingested_ts"] + C.CLOCK_TOLERANCE_MS:
                    bad += 1
                if r["collection_epoch_id"] != C.PREFLIGHT_EPOCH_ID:
                    raise D.Refused("non-preflight record written during PREFLIGHT")
        rep["timestamp_check"] = {"rows": total, "violations": bad}
        # sealing works on the PREFLIGHT namespace (disposable)
        day = D.utc_day(S.now_ms())
        m = await store.seal_day("oi_snapshots", day, C.PREFLIGHT_EPOCH_ID)
        try:
            await store.insert("oi_snapshots", [dict((await store.rows("oi_snapshots"))[0], bucket_ts=1)])
            rep["seal_enforced"] = False
        except D.Refused:
            rep["seal_enforced"] = True
        rep["seal_manifest"] = m
    finally:
        await rest.close()
        await store.close()
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    wall = time.time() - t_start
    rep["resources"] = {"cpu_s": round((ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime), 2),
                        "wall_s": round(wall, 1), "cpu_fraction": round(((ru1.ru_utime + ru1.ru_stime) -
                                                                         (ru0.ru_utime + ru0.ru_stime)) / wall, 3),
                        "max_rss_mb": round(ru1.ru_maxrss / 1024, 1)}
    return rep


def storage_budget(rep: dict) -> dict:
    sz = rep["table_sizes"]
    dur = rep["ws_seconds"]
    per_day = {"oi_snapshots": 12 * 1440, "funding_live_snapshots": 12 * 1440, "mark_index_snapshots": 12 * 1440,
               "book_feature_snapshots": 12 * 86400 // C.SAMPLING_POLICY["book_feature_snapshot_s"],
               "trade_flow_1m": 12 * 1440, "trade_events": int(rep["trades_per_sec_all_symbols"] * 86400)}
    out = {"basis": f"measured over a {dur}s live PREFLIGHT; bytes/row from pg_total_relation_size (incl. indexes)",
           "tables": {}}
    tot_rows = tot_b = 0
    for t, n in per_day.items():
        bpr = (sz.get(t) or {}).get("bytes_per_row") or 600.0
        b = n * bpr
        out["tables"][t] = {"rows_per_day": n, "bytes_per_row": round(bpr, 1), "mb_per_day": round(b / 1e6, 2),
                            "gb_per_year": round(b * 365 / 1e9, 2)}
        tot_rows += n
        tot_b += b
    out["total"] = {"rows_per_day": tot_rows, "mb_per_day": round(tot_b / 1e6, 1), "gb_per_month": round(tot_b * 30 / 1e9, 2),
                    "gb_per_year": round(tot_b * 365 / 1e9, 1)}
    out["raw_book_deltas_not_persisted"] = {"measured_updates_per_sec_per_symbol": rep["book_updates_per_sec_per_symbol"],
                                            "would_be_gb_per_year_at_150B_per_row":
                                                round(rep["book_updates_per_sec_per_symbol"] * 12 * 86400 * 150 * 365 / 1e9, 1)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--code-sha", required=True)
    ap.add_argument("--db-url", default=None)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--tests-passed", default="unknown")
    a = ap.parse_args(argv)
    out = Path(a.out)
    shas = static_artifacts(out)
    rep, budget, errors = None, None, []
    if a.db_url:
        try:
            rep = asyncio.run(live_preflight(a.db_url, a.seconds))
            budget = storage_budget(rep)
        except (D.Refused, S.SourceUnavailable, OSError, asyncio.TimeoutError, RuntimeError, ValueError, KeyError,
                TypeError) as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
    (out / "preflight_report.json").write_text(json.dumps({"report": rep, "errors": errors}, indent=1, sort_keys=True,
                                                          default=str) + "\n")
    (out / "storage_budget.json").write_text(json.dumps(budget or {"status": "NOT_MEASURED"}, indent=1, sort_keys=True) + "\n")
    checks = {}
    if rep:
        checks = {"public_rest_reachable": rep["health"]["rest"]["successes"] > 0 if "rest" in rep["health"] else True,
                  "symbol_mapping_ok": rep["mapping_ok"], "clock_skew_ok": not rep["clock"]["alarm"],
                  "trades_received": rep["messages_per_sec"]["trades"] > 0, "book_messages": rep["messages_per_sec"]["book"] > 0,
                  "books_valid_at_end": all(v == "VALID" for v in rep["book_states_end"].values()),
                  "db_writes": sum(rep["rows_preflight_only"].values()) > 0,
                  "no_prospective_rows": sum(rep["rows_prospective_epoch"].values()) == 0,
                  "timestamps_ok": rep["timestamp_check"]["violations"] == 0, "seal_enforced": rep["seal_enforced"],
                  "tests_passed": a.tests_passed == "true",
                  "storage_within_budget": bool(budget) and budget["total"]["gb_per_year"] <= STORAGE_BUDGET_GB_PER_YEAR}
    ready = bool(checks) and all(checks.values()) and not errors
    summary = {"phase_8h_result": "COLLECTOR_READY_FOR_DEPLOYMENT" if ready else "COLLECTOR_NOT_READY",
               "code_sha": a.code_sha, **shas, "preflight_checks": checks, "errors": errors,
               "sources_implemented": ["open_interest (REST 60s)", "predicted/current funding (REST 60s)",
                                       "mark + index price (REST 60s)", "executed trades (WS) + exact 1m flow",
                                       "level2 book (WS + REST resync) -> 5s feature snapshots"],
               "book_updates_per_second_measured": rep and rep["book_updates_per_sec_per_symbol"],
               "storage": budget and budget["total"], "db_authority": C.DB_AUTHORITY,
               "execution_credentials_present": False, "execution_client_present": False,
               "execution_lease_acquired": False, "orders_sent": 0, "deployed": False, "railway_project": None,
               "railway_service": None, "collector_mode": "NOT_DEPLOYED", "t0_prospective_collection": None,
               "prospective_records_after_t0": 0,
               "preflight_records": rep and sum(rep["rows_preflight_only"].values()),
               "note": "CI/disposable-DB preflight only; deployment to BGX-RESEARCH was not authorized"}
    (out / "phase8h_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(summary, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
