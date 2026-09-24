"""Phase 7D: forward evidence collector/resolver, durable isolated evidence DB,
forward contract V2 + artifact builder, deployable SHADOW bundle, dataset
manifest identity, hook-policy portfolio and bootstrap resampling units.
"""
import copy
import glob
import json
import os
import random
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bot import nexus_oos_inference as inf
from bot import nexus_oos_real_replay as rp
from bot import nexus_oos_replay_manifest as rm
from bot.ai import aggregate_bootstrap as agg
from bot.ai import bundle_export as bx
from bot.ai import evidence_store as es
from bot.ai import forward_artifact as fa
from bot.ai import forward_collector as fc
from bot.ai import forward_evidence as fe
from bot.ai import hook as hk
from bot.ai import shadow_observer as so
from bot.ai import training as tr
from tests.test_ai_decision_authority import DECISION_TS
from tests.test_ai_phase7b import (_WF, observer_pins, observer_runtime, passing_artifact, run,
                                  runtime_with_bundle, synthetic_rows)
from tests.test_ai_phase7c import SYM, fake_decide, market, signal

M15 = 15 * 60_000
UNIVERSE = " ".join(fe.SHADOW_UNIVERSE)
ENV = {"AI_EXECUTION_MODE": "SHADOW", "SHADOW_SYMBOLS": UNIVERSE, "CANDIDATE_SHA": "c" * 40, **observer_pins()}
EXIT = None


def setUpModule():
    global EXIT
    from bot.runtime_bootstrap import install
    install()
    EXIT = rm.load().exit_policy()


def bars_from(start_ts, closes, *, spread=0.002, gap_at=None):
    out, prev = [], closes[0]
    for k, c in enumerate(closes):
        if gap_at is not None and k == gap_at:
            prev = c
            continue
        o = prev
        out.append({"ts": start_ts + k * M15, "o": o, "h": max(o, c) * (1 + spread),
                    "l": min(o, c) * (1 - spread), "c": c, "v": 100.0})
        prev = c
    return out


def record(**over):
    rec = {"candidate_id": "cand-1", "event_ts": DECISION_TS, "symbol": SYM, "direction": "LONG",
           "entry": 100.0, "signal_stop": 98.0, "final_tp": 106.0, "ai_decision": "ABSTAIN",
           "bundle_sha256": "b" * 64, "policy_sha256": "p" * 64, "code_sha": "c" * 40,
           "probability_calibrated": 0.4, "predicted_net_r": -0.1, "regime": "RANGE",
           "decision_latency_ms": 1.0, "decision_candidate_sha": "c" * 40,
           "assumptions": {"fee_rate": 0.0006, "slippage_rate": 0.0005}}
    rec.update(over)
    return rec


def fixture_identity(**over):
    ident = {"contract_name": fe.SHADOW_CONTRACT["name"], "contract_sha256": fe.CONTRACT_SHA256["SHADOW"],
             "code_sha": "c" * 40, "bundle_sha256": "b" * 64, "policy_sha256": "p" * 64,
             "symbol_universe": list(fe.SHADOW_UNIVERSE),
             "cost_identity": {"taker_fee": 0.0006, "slippage_rates": {s: 0.0005 for s in fe.SHADOW_UNIVERSE}}}
    ident.update(over)
    return ident


def mk_window(st, start=DECISION_TS, **over):
    """A committed window whose identity matches record()."""
    return run(st.create_window(fixture_identity(**over), created_at_ms=start - 1, first_boundary_ms=start,
                                window_end_ms=start + fe.WINDOW_DURATION_MS,
                                expected_boundaries=fe.EXPECTED_BOUNDARIES))["window_id"]


def observer(store=None, *, p=0.62, gross_r=0.5):
    r = observer_runtime(p=p, gross_r=gross_r)
    st = store or es.MemoryEvidenceStore(allow_non_durable_for_tests=True)
    o = so.ShadowObserver(ENV, client=object(), runtime=r, manifest=rm.load(), store=st)
    o.mmr_proxy[SYM] = 0.004
    o.start()
    run(o.open_window(DECISION_TS - 1))
    return o, st


def observe(o):
    with patch("bot.strategy.Analyzer.analyze_mtf", lambda self, *a, **k: signal(sl=99.5, tp=101.5)), \
            patch("bot.nexus_ai.decide", fake_decide()):
        return run(o.evaluate(SYM, *market(), decision_ts=DECISION_TS, now_ms=DECISION_TS + 60_000))


# ── forward collection ───────────────────────────────────────────────────────
class ForwardCollection(unittest.TestCase):
    def test_hook_candidate_is_persisted_once(self):
        o, st = observer()
        a, b = observe(o), observe(o)
        self.assertEqual(a["stage"], "AI_HOOK", a)
        self.assertEqual(a["persisted"], "INSERTED")
        self.assertEqual(b["persisted"], "IDEMPOTENT_DUPLICATE")       # same candidate, no duplicate
        rows = run(st.candidates(o.window["window_id"]))
        self.assertEqual(len(rows), 1)
        rec = rows[0]
        for k in ("candidate_id", "decision_id", "event_ts", "symbol", "direction", "code_sha", "bundle_sha256",
                  "policy_sha256", "feature_schema_sha256", "hook_population", "hook_profile", "entry",
                  "signal_stop", "signal_tp", "planned_rr", "strategy_score", "nexus_confidence", "regime",
                  "feature_hash", "probability_raw", "probability_calibrated", "predicted_gross_r",
                  "predicted_net_r", "ai_decision", "vetoes", "reason_codes", "assumptions", "status"):
            self.assertIn(k, rec)
        self.assertEqual(rec["status"], "PENDING")
        self.assertEqual(rec["hook_profile"], hk.PROFILE_LIVE_PILOT)

    def test_abstain_and_trade_candidates_both_get_outcomes(self):
        for p, gross, expected in ((0.3, 0.5, "ABSTAIN"), (0.62, 2.0, "TRADE")):
            o, st = observer(p=p, gross_r=gross)
            observe(o)
            wid = o.window["window_id"]
            rec = run(st.candidates(wid))[0]
            self.assertEqual(rec["ai_decision"], expected)
            bars = bars_from(DECISION_TS, [100.0, 100.2, 100.6, 101.0, 101.6, 102.0])

            async def fb(*a):
                return bars

            async def ff(*a):
                return []
            res = run(fc.resolve_pending(st, wid, fb, ff, now_ms=DECISION_TS + 7 * M15, exit_policy=EXIT))
            self.assertEqual(res["resolved"], 1, (expected, res))
            done = run(st.candidates(wid))[0]
            self.assertEqual(done["status"], "RESOLVED")
            self.assertIsNotNone(done["outcome"]["modeled_net_r"])
            self.assertEqual(done["ai_decision"], expected)               # never relabelled


class Resolver(unittest.TestCase):
    def test_uses_only_candles_closed_by_evaluation_time(self):
        future = bars_from(DECISION_TS, [100.0, 100.1, 100.2, 107.0])      # TP only in the 4th bar
        st, out = fc.resolve_one(record(), future, [], now_ms=DECISION_TS + 3 * M15, exit_policy=EXIT)
        self.assertEqual((st, out), ("PENDING", None))                     # bar 4 not closed yet
        st2, out2 = fc.resolve_one(record(), future, [], now_ms=DECISION_TS + 4 * M15, exit_policy=EXIT)
        self.assertEqual(st2, "RESOLVED")
        self.assertEqual(out2["resolved_with_bars_closed_by_ms"], DECISION_TS + 4 * M15)

    def test_exit_semantics_equal_the_replay_parity_function(self):
        rng = random.Random(3)
        for path in ([100.0, 101.0, 102.2, 101.0, 99.9, 99.0],                # TP1 partial then BE
                     [100.0, 101.5, 103.0, 104.5, 106.5],                     # trailing / 2R / TP
                     [100.0, 99.5, 98.5, 97.0],                               # stop
                     [100.0] + [100 + rng.gauss(0, 1.5) for _ in range(60)]):
            bars = bars_from(DECISION_TS, path)
            events = [{"timepoint": DECISION_TS + 2 * M15 + 1, "fundingRate": 0.001}]
            st, out = fc.resolve_one(record(), bars, events, now_ms=DECISION_TS + len(bars) * M15, exit_policy=EXIT)
            if st != "RESOLVED":
                continue
            from bot.backtest import _timestamp_index
            ref = rp._parity_outcome({"k15": bars, "ts15": _timestamp_index(bars), "funding_events": events,
                                      "funding_ts": [e["timepoint"] for e in events], "exit_policy": EXIT},
                                     direction="LONG", i=0, sig_entry=100.0, sl=98.0, tp=106.0,
                                     fee_rate=0.0006, slip=0.0005)
            self.assertAlmostEqual(out["modeled_net_r"], ref["r"], places=12)
            self.assertEqual(out["exit_reason"], ref["sim"]["exit_reason"])
            self.assertEqual(out["legs"], [list(x) for x in ref["sim"]["legs"]])
        partial = fc.resolve_one(record(), bars_from(DECISION_TS, [100.0, 101.0, 102.2, 101.0, 99.9, 99.0]), [],
                                 now_ms=DECISION_TS + 6 * M15, exit_policy=EXIT)[1]
        self.assertGreaterEqual(len(partial["legs"]), 2)                 # 1R partial + remainder

    def test_funding_and_cost_stress_recorded(self):
        bars = bars_from(DECISION_TS, [100.0] + [100.0 + 0.05 * k for k in range(1, 40)] + [107.0])
        events = [{"timepoint": DECISION_TS + 5 * M15, "fundingRate": 0.002}]
        st, out = fc.resolve_one(record(), bars, events, now_ms=DECISION_TS + len(bars) * M15, exit_policy=EXIT)
        self.assertEqual(st, "RESOLVED")
        self.assertNotEqual(out["funding_r"], 0)
        self.assertEqual(set(out["cost_stress_r"]), set(rp.COST_SCENARIOS))
        self.assertLess(out["cost_stress_r"]["combined_adverse"], out["cost_stress_r"]["current"])
        for k in ("outcome_end_ts", "exit_reason", "gross_r", "fees_r", "slippage_r", "funding_r",
                  "modeled_net_r", "mfe_r", "mae_r", "holding_time_ms", "legs"):
            self.assertIn(k, out)

    def test_unresolved_stays_pending_and_window_end_censors(self):
        st = es.MemoryEvidenceStore(allow_non_durable_for_tests=True)
        wid = mk_window(st)
        run(st.put_candidate(wid, record()))
        bars = bars_from(DECISION_TS, [100.0, 100.1, 100.05, 100.1])

        async def fb(*a):
            return bars

        async def ff(*a):
            return []
        res = run(fc.resolve_pending(st, wid, fb, ff, now_ms=DECISION_TS + 10 * M15, exit_policy=EXIT))
        self.assertEqual(res["pending"], 1)
        self.assertEqual(run(st.get("cand-1"))["status"], "PENDING")
        closed = run(fc.close_window(st, wid, fb, ff, exit_policy=EXIT,
                                     now_ms=DECISION_TS + fe.WINDOW_DURATION_MS))
        self.assertEqual(closed["right_censored_data_end"], 1)
        rec = run(st.get("cand-1"))
        self.assertEqual(rec["status"], "RIGHT_CENSORED_DATA_END")
        self.assertNotIn("modeled_net_r", rec["outcome"])                  # no forced close / no R

    def test_gap_rule_and_missing_decision_bar(self):
        gapped = bars_from(DECISION_TS, [100.0, 100.1, 100.0, 100.1, 100.2], gap_at=2)
        st, out = fc.resolve_one(record(), gapped, [], now_ms=DECISION_TS + 6 * M15, exit_policy=EXIT)
        self.assertEqual(st, "RIGHT_CENSORED_DATA_GAP")
        self.assertEqual(out["reason"], "GAP_RULE")
        late = bars_from(DECISION_TS + M15, [100.0, 100.1, 100.2, 100.3, 100.4])
        self.assertEqual(fc.resolve_one(record(), late, [], now_ms=DECISION_TS + 8 * M15, exit_policy=EXIT)[0],
                         "INVALID")

    def test_restart_resumes_pending_and_resolver_is_idempotent(self):
        st = es.MemoryEvidenceStore(allow_non_durable_for_tests=True)
        o, _ = observer(st)
        observe(o)
        o2, _ = observer(st)                                                # "restart" on the same store
        wid = o2.window["window_id"]
        self.assertEqual(wid, o.window["window_id"])                        # same durable window
        self.assertEqual(len(run(st.candidates(wid, "PENDING"))), 1)
        bars = bars_from(DECISION_TS, [100.0, 100.2, 100.6, 101.0, 101.6, 102.0])

        async def fb(*a):
            return bars

        async def ff(*a):
            return []
        first = run(fc.resolve_pending(st, wid, fb, ff, now_ms=DECISION_TS + 7 * M15, exit_policy=EXIT))
        rec = run(st.candidates(wid))[0]
        second = run(fc.resolve_pending(st, wid, fb, ff, now_ms=DECISION_TS + 9 * M15, exit_policy=EXIT))
        self.assertEqual((first["resolved"], second["resolved"], second["pending"]), (1, 0, 0))
        self.assertEqual(run(st.candidates(wid))[0]["record_sha256"], rec["record_sha256"])
        self.assertFalse(run(st.finalize(rec["candidate_id"], "RESOLVED", {"x": 1})))

    def test_tampered_pending_row_fails_verification(self):
        st = es.MemoryEvidenceStore(allow_non_durable_for_tests=True)
        wid = mk_window(st)
        run(st.put_candidate(wid, record()))
        w, status, k, raw = st.t["ai_shadow_candidates"]["cand-1"]
        d = json.loads(raw)
        d["entry"] = 90.0
        st.t["ai_shadow_candidates"]["cand-1"] = (w, status, k, json.dumps(d))
        with self.assertRaises(Exception):
            run(st.candidates(wid, "PENDING"))

    def test_db_outage_breaks_continuity(self):
        st = es.MemoryEvidenceStore(allow_non_durable_for_tests=True)
        wid = mk_window(st)
        st.fail = True
        with self.assertRaises(es.EvidenceContinuityBroken):
            run(st.put_candidate(wid, record()))
        st.fail = False
        with self.assertRaises(es.EvidenceContinuityBroken):              # halted, not silently resumed
            run(st.put_candidate(wid, record()))


# ── evidence DB authority ────────────────────────────────────────────────────
GOOD = {"EVIDENCE_DATABASE_URL": "postgresql://u@research-db:5432/evidence",
        "EVIDENCE_DB_AUTHORITY_ID": "bgx-research-evidence",
        "PRODUCTION_DB_AUTHORITY_ID": "bgx-prod-exec", "PRODUCTION_DB_FINGERPRINT": "0123456789abcdef"}


class EvidenceDatabase(unittest.TestCase):
    def test_dedicated_research_db_is_accepted(self):
        ident = es.preflight(GOOD)
        self.assertEqual(ident["authority_id"], "bgx-research-evidence")
        self.assertNotIn("u@", json.dumps(ident))                         # no credentials

    def test_sqlite_and_missing_postgres_are_refused(self):
        for url in ("", "sqlite:////tmp/bgx_capital.db", "/tmp/bgx_capital.db"):
            with self.assertRaises(es.EvidenceStoreRefused):
                es.preflight({**GOOD, "EVIDENCE_DATABASE_URL": url})
        with self.assertRaises(es.EvidenceStoreRefused):
            run(es.MemoryEvidenceStore().init())                          # non-durable backend
        r, _ = runtime_with_bundle("SHADOW")
        o = so.ShadowObserver(ENV, client=object(), runtime=r, manifest=rm.load(),
                              store=es.MemoryEvidenceStore())
        with self.assertRaises(so.ObserverRefused):
            o.start()

    def test_postgres_unavailable_refuses_evidence_mode(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        env = {**GOOD, "EVIDENCE_DATABASE_URL": f"postgresql://nobody@127.0.0.1:{port}/none"}
        with self.assertRaises(es.EvidenceStoreRefused):
            run(es.EvidenceStore.connect(env))

    def test_production_db_identity_is_refused(self):
        for bad in ({"EVIDENCE_DB_AUTHORITY_ID": "bgx-prod-exec"},
                    {"PRODUCTION_DB_FINGERPRINT": es.fingerprint(GOOD["EVIDENCE_DATABASE_URL"])},
                    {"DATABASE_URL": "postgresql://other@research-db:5432/evidence"},
                    {"PRODUCTION_DB_AUTHORITY_ID": ""}, {"EVIDENCE_DB_AUTHORITY_ID": "bad id!"}):
            with self.assertRaises(es.EvidenceStoreRefused, msg=bad):
                es.preflight({**GOOD, **bad})


def _pg_cluster():
    """Throw-away local PostgreSQL (tests only); None when unavailable."""
    initdb = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))
    if not initdb:
        return None
    bindir = os.path.dirname(initdb[-1])
    root = os.geteuid() == 0
    tmp = tempfile.mkdtemp(prefix="bgxpg")
    user = "postgres" if root else os.environ.get("USER") or "runner"
    pre = ["runuser", "-u", "postgres", "--"] if root else []
    if root:
        shutil.chown(tmp, "postgres")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    data = os.path.join(tmp, "data")
    try:
        subprocess.run(pre + [f"{bindir}/initdb", "-D", data, "-U", user, "--auth=trust"], check=True,
                       capture_output=True, timeout=120)
        subprocess.run(pre + [f"{bindir}/pg_ctl", "-D", data, "-l", os.path.join(tmp, "log"), "-w", "-o",
                              f"-p {port} -k {tmp} -c listen_addresses=127.0.0.1", "start"],
                       check=True, capture_output=True, timeout=120)
    except Exception:
        return None
    return {"url": f"postgresql://{user}@127.0.0.1:{port}/postgres", "stop": lambda: subprocess.run(
        pre + [f"{bindir}/pg_ctl", "-D", data, "-m", "immediate", "stop"], capture_output=True, timeout=60)}


class RealPostgresEvidenceStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = _pg_cluster()
        if cls.pg is None:
            raise unittest.SkipTest("no local PostgreSQL binaries")

    @classmethod
    def tearDownClass(cls):
        cls.pg["stop"]()

    def env(self):
        return {**GOOD, "EVIDENCE_DATABASE_URL": self.pg["url"]}

    def test_roundtrip_idempotency_digest_and_marker(self):
        async def go():
            st = await es.EvidenceStore.connect(self.env())
            self.assertEqual(st.backend, "postgresql")
            wid = (await st.create_window(fixture_identity(), created_at_ms=DECISION_TS - 1,
                                          first_boundary_ms=DECISION_TS,
                                          window_end_ms=DECISION_TS + fe.WINDOW_DURATION_MS,
                                          expected_boundaries=fe.EXPECTED_BOUNDARIES))["window_id"]
            self.assertEqual(await st.put_candidate(wid, record(candidate_id="pg-1")), "INSERTED")
            self.assertEqual(await st.put_candidate(wid, record(candidate_id="pg-1")), "IDEMPOTENT_DUPLICATE")
            self.assertEqual([r["candidate_id"] for r in await st.candidates(wid, "PENDING")], ["pg-1"])
            self.assertTrue(await st.finalize("pg-1", "RESOLVED", {"modeled_net_r": 0.5}))
            self.assertFalse(await st.finalize("pg-1", "RESOLVED", {"modeled_net_r": 9}))
            self.assertEqual((await st.get("pg-1"))["outcome"], {"modeled_net_r": 0.5})
            await st.heartbeat(wid, {"ts": DECISION_TS, "kind": "SCAN"})
            self.assertEqual(len(await st.heartbeats(wid)), 1)
            # restart: a second connection sees the same durable state
            st2 = await es.EvidenceStore.connect(self.env())
            self.assertEqual((await st2.get("pg-1"))["status"], "RESOLVED")
            await st2.conn.execute("UPDATE ai_shadow_candidates SET record = replace(record, '0.5', '0.7')")
            with self.assertRaises(Exception):
                await st2.get("pg-1")
            await st2.conn.execute("DELETE FROM ai_shadow_candidates")
            await st.conn.close()
            await st2.conn.close()
        run(go())

    def test_production_state_or_other_role_is_refused(self):
        async def go():
            import asyncpg
            c = await asyncpg.connect(self.pg["url"])
            await c.execute("CREATE DATABASE prodlike")
            await c.close()
            url = self.pg["url"].rsplit("/", 1)[0] + "/prodlike"
            c2 = await asyncpg.connect(url)
            await c2.execute("CREATE TABLE trades (id int)")
            await c2.execute("INSERT INTO trades VALUES (1)")
            await c2.close()
            with self.assertRaises(es.EvidenceStoreRefused):
                await es.EvidenceStore.connect({**self.env(), "EVIDENCE_DATABASE_URL": url})
            st = await es.EvidenceStore.connect(self.env())
            await st.conn.execute("UPDATE ai_evidence_authority SET value='OTHER' WHERE key='role'")
            with self.assertRaises(es.EvidenceStoreRefused):
                await es.EvidenceStore.connect(self.env())
            await st.conn.execute(f"UPDATE ai_evidence_authority SET value='{es.ROLE}' WHERE key='role'")
            await st.conn.close()
        run(go())


# ── research semantics ───────────────────────────────────────────────────────
class DatasetManifest(unittest.TestCase):
    def test_manifest_is_the_exact_training_population(self):
        rows = synthetic_rows(n=200)
        for r in rows:
            r["ai_feature_hash"] = f"h{r['ts']}"
        extra = copy.deepcopy(rows[:5])
        for r in extra:
            r["ai_hook_eligible"] = False                                   # executable, not hook
            r["ts"] += 1
        m = tr.dataset_manifest(rows)
        self.assertEqual(tr.dataset_manifest(rows + extra)["training_dataset_sha256"], m["training_dataset_sha256"])
        self.assertEqual(m["training_dataset_rows"], len(tr.dataset(rows)))
        self.assertEqual((m["training_population"], m["training_hook_profile"]), (hk.POPULATION, hk.TRAINING_PROFILE))
        self.assertNotEqual(tr.dataset_manifest(rows[1:])["training_dataset_sha256"], m["training_dataset_sha256"])
        changed = copy.deepcopy(rows)
        changed[7]["ai_feature_hash"] = "other"
        self.assertNotEqual(tr.dataset_manifest(changed)["training_dataset_sha256"], m["training_dataset_sha256"])

    def test_walk_forward_binds_its_own_manifest(self):
        passing_artifact()
        wf = _WF["wf"]
        self.assertEqual(wf["dataset_manifest"], tr.dataset_manifest(synthetic_rows()))


class BootstrapUnits(unittest.TestCase):
    def _section(self, zero_a_blocks=60, active=40):
        rng = random.Random(4)
        ids = list(range(1000, 1000 + active + zero_a_blocks))
        a_cnt = [rng.randint(1, 3) if k < active else 0 for k in range(len(ids))]
        rng.shuffle(a_cnt)
        b_cnt = [c + rng.randint(1, 4) for c in a_cnt]
        a_sum = [round(sum(rng.gauss(0.2, 1) for _ in range(c)), 12) for c in a_cnt]
        b_sum = [round(a + sum(rng.gauss(-0.1, 1) for _ in range(bc - ac)), 12)
                 for a, ac, bc in zip(a_sum, a_cnt, b_cnt)]
        series = {"block_ids": ids, "a_sum": a_sum, "a_count": a_cnt, "b_sum": b_sum, "b_count": b_cnt}
        _, _, point = inf.influence_scores(series, diff=True)
        return series, point

    def test_zero_a_blocks_stay_in_the_resampling_universe(self):
        series, point = self._section()
        day = inf.DAY_MS
        sec = {"delta": point, "block_intervals": [
            {"block_ms": ms, "ci": [0, 0], "residual_dependence": {"residual_series_kind": inf.SERIES_KIND_DIFF,
                                                                   "series": series}}
            for ms in inf._lengths(day) if ms >= day]}
        rec = agg.recompute(sec, required_ms=day, min_blocks=30, paired=True, samples=1000)
        self.assertEqual(rec["n_resampling_units"], len(series["block_ids"]))
        self.assertEqual(rec["n_a_active_blocks"], 40)
        self.assertEqual(rec["n_b_active_blocks"], 100)
        full = agg.ci_from_series(series, diff=True, samples=1000)
        keep = [k for k, c in enumerate(series["a_count"]) if c > 0]
        dropped = {k: [v[i] for i in keep] for k, v in series.items()}
        self.assertEqual(rec["intervals"][0]["ci"], list(full))
        self.assertNotEqual(list(full), list(agg.ci_from_series(dropped, diff=True, samples=1000)))


class ForwardContractAndArtifact(unittest.TestCase):
    SAFE = {"exchange_credentials_present": False, "mutating_client_methods": False,
            "execution_lease_acquired": False, "orders_sent": 0}

    def _cands(self, n=40, trades=0):
        out = []
        rng = random.Random(9)
        for k in range(n):
            ts = DECISION_TS + k * 6 * 3_600_000
            r = rng.gauss(-0.1, 1)
            out.append({**record(candidate_id=f"c{k}", event_ts=ts, symbol=fe.SHADOW_UNIVERSE[k % 12],
                                 ai_decision="TRADE" if k < trades else "ABSTAIN",
                                 probability_calibrated=0.3 + 0.01 * (k % 30)),
                        "status": "RESOLVED",
                        "outcome": {"modeled_net_r": r, "outcome_end_ts": ts + 3_600_000,
                                    "cost_stress_r": {n_: r - 0.05 for n_ in rp.COST_SCENARIOS}}})
        return out

    def _identity(self):
        return {"code_sha": "c" * 40, "bundle_sha256": "b" * 64, "policy_sha256": "p" * 64,
                "feature_schema_sha256": "f" * 64, "hook_population": hk.POPULATION,
                "hook_profile": hk.PROFILE_LIVE_PILOT, "symbols": list(fe.SHADOW_UNIVERSE)}

    def test_v2_contract_uses_the_hook_baseline(self):
        c = fe.SHADOW_CONTRACT_V2                                       # superseded by V3 (Phase 7E)
        self.assertEqual(c["name"], "FORWARD_SHADOW_EVIDENCE_V2")
        self.assertEqual(fe.SUPERSEDED[c["name"]]["status"], "SUPERSEDED_BEFORE_FIRST_FORWARD_WINDOW")
        self.assertEqual(fe.SHADOW_CONTRACT["baselines"], c["baselines"])
        self.assertIn("ALL AI_RUNTIME_HOOK_POPULATION_V1", c["baselines"]["HOOK_BASELINE"])
        self.assertIn("NOT MEASURED", c["baselines"]["EFFECTIVE_EXECUTION_BASELINE"])
        self.assertEqual(set(c["products"]), {"FORWARD_OBSERVATION_DATASET", "FROZEN_POLICY_FORWARD_EVIDENCE"})
        self.assertLessEqual(set(c["products"]), set(fe.SHADOW_CONTRACT["products"]))
        self.assertEqual(len(fe.CONTRACT_SHA256["SHADOW"]), 64)
        cands = self._cands(trades=10)
        art = fa._build_forward_shadow_artifact(cands, [], identity=self._identity(), window_start_ms=DECISION_TS,
                                               window_end_ms=None, safety=self.SAFE, journal_verified=True)
        rs = [x["outcome"]["modeled_net_r"] for x in cands]
        self.assertAlmostEqual(art["FROZEN_POLICY_FORWARD_EVIDENCE"]["hook_baseline_mean_r"], sum(rs) / len(rs))
        self.assertEqual(art["FROZEN_POLICY_FORWARD_EVIDENCE"]["baseline"], "HOOK_BASELINE")

    def test_zero_trade_policy_still_builds_the_observation_dataset(self):
        art = fa._build_forward_shadow_artifact(self._cands(trades=0), [], identity=self._identity(),
                                               window_start_ms=DECISION_TS, window_end_ms=None, safety=self.SAFE,
                                               journal_verified=True)
        self.assertEqual(art["FORWARD_OBSERVATION_DATASET"]["resolved_hook_candidates"], 40)
        self.assertIsNotNone(art["FORWARD_OBSERVATION_DATASET"]["calibration"])
        self.assertEqual(art["FROZEN_POLICY_FORWARD_EVIDENCE"]["status"], "INSUFFICIENT_EVIDENCE")
        self.assertIn(art["verdict"], fa.IN_CANDIDATE_VERDICTS)
        self.assertNotEqual(art["verdict"], "PASS")
        closed = fa._build_forward_shadow_artifact(self._cands(trades=0), [], identity=self._identity(),
                                                  window_start_ms=DECISION_TS, window_end_ms=DECISION_TS + 40 * 86_400_000,
                                                  safety=self.SAFE, journal_verified=True)
        self.assertEqual(closed["verdict"], "INSUFFICIENT_EVIDENCE")

    def test_zero_order_violation_or_identity_change_invalidates(self):
        for bad in ({"orders_sent": 1}, {"exchange_credentials_present": True}, {"execution_lease_acquired": True},
                    {"mutating_client_methods": True}):
            art = fa._build_forward_shadow_artifact(self._cands(), [], identity=self._identity(),
                                                   window_start_ms=DECISION_TS, window_end_ms=None,
                                                   safety={**self.SAFE, **bad}, journal_verified=True)
            self.assertEqual(art["verdict"], "BLOCK")
            self.assertIn("ZERO_ORDER_ASSERTION_FAILED", art["blockers"])
        mixed = self._cands()
        mixed[3]["bundle_sha256"] = "x" * 64
        art = fa._build_forward_shadow_artifact(mixed, [], identity=self._identity(), window_start_ms=DECISION_TS,
                                               window_end_ms=None, safety=self.SAFE, journal_verified=True)
        self.assertIn("IDENTITY_CHANGED_MID_WINDOW", art["blockers"])


class ShadowBundleArtifact(unittest.TestCase):
    def _artifact(self):
        passing_artifact()
        wf = _WF["wf"]
        obs = tr.build_shadow_challenger(synthetic_rows(), wf, training_code_sha="c" * 40,
                                         created_at="2026-09-25T00:00:00Z", lifecycle_state="SHADOW_OBSERVER")
        art = {"candidate_sha": "c" * 40, "candidate_research": {"ai_meta_model": {
            "shadow_challenger": {"created": False}, "shadow_observer": {**obs, "claims": {"edge_claim": False}},
            "dataset_manifest": wf["dataset_manifest"]}}}
        return json.loads(json.dumps(art))                               # as downloaded from CI

    def test_bundle_files_export_and_load_through_real_runtime(self):
        tmp = tempfile.mkdtemp()
        res = bx.export(self._artifact(), tmp, candidate_sha="c" * 40)
        self.assertEqual(sorted(os.listdir(tmp)), sorted(bx.BUNDLE_FILES))
        meta = json.load(open(os.path.join(tmp, "bundle_metadata.json")))
        for k in ("candidate_code_sha", "bundle_sha256", "policy_sha256", "feature_schema_sha256", "hook_population",
                  "hook_profile", "lifecycle_state", "dataset_manifest_sha256", "created_at"):
            self.assertIsNotNone(meta[k], k)
        self.assertEqual(meta["lifecycle_state"], "SHADOW_OBSERVER")
        v = bx.verify(tmp)
        self.assertTrue(v["verified"])
        self.assertEqual(v["bundle_sha256"], res["bundle_sha256"])
        from bot.ai import runtime as rt
        b = rt.load_bundle({"AI_BUNDLE_DIR": tmp, "AI_BUNDLE_SHA256": res["bundle_sha256"]})
        self.assertEqual(b.manifest["hook_profile"], hk.TRAINING_PROFILE)
        dm = bx.deploy_manifest(res)
        self.assertFalse(dm["secrets_included"])
        self.assertEqual(dm["forward_contract"], "FORWARD_SHADOW_EVIDENCE_V3")
        self.assertEqual(dm["symbol_universe"], list(fe.SHADOW_UNIVERSE))
        text = json.dumps(dm)
        for secret in ("postgresql://", "API_SECRET=", "PASSPHRASE="):
            self.assertNotIn(secret, text)

    def test_wrong_hash_or_extra_file_fails(self):
        tmp = tempfile.mkdtemp()
        bx.export(self._artifact(), tmp, candidate_sha="c" * 40)
        with self.assertRaises(Exception):
            bx.verify(tmp, pinned_bundle_sha="0" * 64)
        m = json.load(open(os.path.join(tmp, "manifest.json")))
        m["decision_policy"]["min_p_profitable"] = 0.01
        json.dump(m, open(os.path.join(tmp, "manifest.json"), "w"))
        with self.assertRaises(Exception):
            bx.verify(tmp)
        tmp2 = tempfile.mkdtemp()
        bx.export(self._artifact(), tmp2, candidate_sha="c" * 40)
        open(os.path.join(tmp2, "extra.txt"), "w").write("x")
        with self.assertRaises(bx.BundleExportError):
            bx.verify(tmp2)

    def test_no_bundle_no_export(self):
        with self.assertRaises(bx.BundleExportError):
            bx.select_bundle({"candidate_research": {"ai_meta_model": {"status": "OK"}}})


class ObserverSafety(unittest.TestCase):
    def test_twelve_symbol_universe_required(self):
        self.assertEqual(len(fe.SHADOW_UNIVERSE), 12)
        for syms in ("BTCUSDT ETHUSDT", UNIVERSE.replace("ATOMUSDT", ""), UNIVERSE + " PEPEUSDT"):
            with self.assertRaises(so.ObserverRefused):
                so.ShadowObserver({**ENV, "SHADOW_SYMBOLS": syms}, client=object(), manifest=rm.load())
        so.ShadowObserver(ENV, client=object(), manifest=rm.load())

    def test_zero_order_capability_and_orders_sent_is_immutable(self):
        o, _ = observer()
        self.assertEqual(o.safety(), {"exchange_credentials_present": False, "mutating_client_methods": False,
                                      "execution_lease_acquired": False, "orders_sent": 0})
        with self.assertRaises(so.ObserverRefused):
            o.orders_sent = 1
        self.assertEqual(o.orders_sent, 0)
        st = o.status()
        for k in ("observer_running", "last_successful_scan_ms", "db_durable", "bundle_verified", "policy_verified",
                  "schema_verified", "symbols_configured", "pending_candidates", "resolved_candidates", "data_gaps"):
            self.assertIn(k, st)
        self.assertEqual(st["orders_sent"], 0)

    def test_observer_never_touches_the_production_database_layer(self):
        import inspect
        src = inspect.getsource(so)
        for bad in ("from bot import database", "import bot.database", "db.init(", "DurableAIJournal()"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
