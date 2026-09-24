"""Phase 7E: prospective window lock, data completeness and evidence continuity.

FORWARD_SHADOW_EVIDENCE_V3: one durable immutable window per evidence DB
(fixed 30 days, bounds derived from the DB), boundary journal for all 12
pinned symbols, exact decision candle, payload-hash duplicate detection,
durable continuity, cost identity, window close, artifact from the store.
"""
import glob
import inspect
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from bot import nexus_oos_replay_manifest as rm
from bot.ai import evidence_store as es
from bot.ai import forward_artifact as fa
from bot.ai import forward_collector as fc
from bot.ai import forward_evidence as fe
from bot.ai import shadow_observer as so
from bot.ai.runtime import JournalIntegrityError
from tests.test_ai_decision_authority import DECISION_TS
from tests.test_ai_phase7b import observer_pins, observer_runtime, run, runtime_with_bundle
from tests.test_ai_phase7c import SYM, fake_decide, market, signal
from tests.test_ai_phase7d import GOOD, _pg_cluster, bars_from, mk_window, record, fixture_identity

M15 = fe.M15_MS
ENV = {"AI_EXECUTION_MODE": "SHADOW", "SHADOW_SYMBOLS": " ".join(fe.SHADOW_UNIVERSE), "CANDIDATE_SHA": "c" * 40,
       **observer_pins()}
EXIT = None
STEPS = ["NO_CREDENTIALS", "READ_ONLY_CLIENT", "ORDERED_UNIVERSE", "POSTGRESQL_CONNECTED",
         "DB_ISOLATION_VERIFIED", "BUNDLE_LOADED", "POLICY_SCHEMA_VERIFIED", "CONTRACT_V3_VERIFIED",
         "REPLAY_MANIFEST_RUNTIME_PARITY", "CODE_PROVENANCE_VERIFIED", "WINDOW_COMMITTED"]


def setUpModule():
    global EXIT
    from bot.runtime_bootstrap import install
    install()
    EXIT = rm.load().exit_policy()


def mem():
    return es.MemoryEvidenceStore(allow_non_durable_for_tests=True)


def make(store=None, *, env=None, runtime=None, p=0.62, gross_r=0.5):
    """Observer through steps 1-8 (no window yet)."""
    r = runtime or observer_runtime(p=p, gross_r=gross_r)
    st = store if store is not None else mem()
    o = so.ShadowObserver(env or ENV, client=object(), runtime=r, manifest=rm.load(), store=st)
    o.mmr_proxy[SYM] = 0.004
    o.start()
    return o, st


def opened(store=None, *, start=DECISION_TS, **kw):
    o, st = make(store, **kw)
    run(o.open_window(start - 1))
    return o, st


def wire(o, *, k=None, on_symbol=None):
    """Offline market for run_once: every symbol gets the fixture candles."""
    data = k or market()

    async def fetch(symbol):
        if on_symbol:
            on_symbol(symbol)
        return data

    async def funding(*a):
        return []

    async def bars(*a):
        return bars_from(DECISION_TS, [100.0, 100.05, 100.1, 100.05])
    o._fetch, o._funding_since, o._bars_since = fetch, funding, bars


def patched():
    sig = signal(sl=99.5, tp=101.5)
    return (patch("bot.strategy.Analyzer.analyze_mtf",
                  lambda self, symbol, *a, **k: sig if symbol == SYM else None),
            patch("bot.nexus_ai.decide", fake_decide()))


def run_boundary(o, now):
    a, b = patched()
    with a, b:
        return run(o.run_once(now))


def observe(o):
    a, b = patched()
    with a, b:
        return run(o.evaluate(SYM, *market(), decision_ts=DECISION_TS, now_ms=DECISION_TS + 60_000))


# ── window identity, creation and bounds ────────────────────────────────────
class WindowLock(unittest.TestCase):
    def test_window_created_before_first_candidate_and_startup_order(self):
        o, st = make()
        with self.assertRaises(so.ObserverRefused):
            observe(o)                                               # no committed window => no evaluation
        self.assertEqual(st.t["ai_shadow_candidates"], {})
        with self.assertRaises(Exception):
            run(st.put_candidate("no-such-window", record()))      # FK: a candidate needs a window
        run(o.open_window(DECISION_TS - 1))
        self.assertEqual(o.steps, STEPS)
        r = observe(o)
        self.assertEqual(r["persisted"], "INSERTED")
        rec = run(st.candidates(o.window["window_id"]))[0]
        self.assertEqual(rec["window_id"], o.window["window_id"])

    def test_window_refused_before_startup_sequence(self):
        r, _ = runtime_with_bundle("SHADOW")
        o = so.ShadowObserver(ENV, client=object(), runtime=r, manifest=rm.load(), store=mem())
        with self.assertRaises(so.ObserverRefused):
            run(o.open_window(DECISION_TS))

    def test_start_is_next_boundary_and_end_is_exactly_30_days(self):
        for now, start in ((DECISION_TS + 7 * 60_000, DECISION_TS + M15), (DECISION_TS, DECISION_TS + M15),
                           (DECISION_TS - 1, DECISION_TS)):
            o, st = make()
            w = run(o.open_window(now))
            self.assertEqual(w["window_start_ms"], start)
            self.assertEqual(w["first_boundary_ms"], start)
            self.assertEqual(w["window_end_ms"] - w["window_start_ms"], 30 * 86_400_000)
            self.assertEqual(w["expected_boundaries"], 2880)
            self.assertEqual(w["created_at_ms"], now)
            self.assertEqual(w["identity"]["contract_name"], "FORWARD_SHADOW_EVIDENCE_V3")

    def test_bounds_not_caller_overridable(self):
        o, st = opened()
        wid = o.window["window_id"]
        for field in ("window_start_ms", "window_end_ms", "first_boundary_ms", "expected_boundaries",
                      "identity", "window_id", "created_at_ms"):
            with self.assertRaises(es.WindowRefused, msg=field):
                run(st.update_window(wid, **{field: 1}))
        self.assertEqual(list(inspect.signature(o.open_window).parameters), ["now_ms"])
        self.assertEqual(list(inspect.signature(fa.build_forward_shadow_artifact_from_store).parameters),
                         ["store", "window_id"])
        self.assertFalse(hasattr(fa, "build_forward_shadow_artifact"))     # old caller-bounds builder is private
        # a tampered stored bound is detected by the window digest
        pk = wid
        w, s_, k, raw = st.t["ai_evidence_windows"][pk]
        d = json.loads(raw)
        d["window_end_ms"] += 86_400_000
        st.t["ai_evidence_windows"][pk] = (w, s_, k, json.dumps(d))
        with self.assertRaises(JournalIntegrityError):
            run(st.windows())

    def test_restart_loads_the_same_window(self):
        o, st = opened()
        o2, _ = make(st)
        w2 = run(o2.open_window(DECISION_TS + 3 * 86_400_000))          # much later restart
        self.assertEqual(w2["window_id"], o.window["window_id"])
        self.assertEqual(w2["window_start_ms"], DECISION_TS)                 # clock NOT restarted
        self.assertEqual(len(run(st.windows())), 1)

    def test_identity_changes_cannot_append(self):
        changes = {"code_sha": "d" * 40, "bundle_sha256": "e" * 64, "policy_sha256": "f" * 64,
                   "feature_schema_sha256": "0" * 64, "hook_population": "OTHER", "hook_profile": "OTHER",
                   "contract_sha256": "1" * 64, "contract_name": "FORWARD_SHADOW_EVIDENCE_V2",
                   "symbol_universe": list(reversed(fe.SHADOW_UNIVERSE)),
                   "evidence_db_authority_id": "other-db"}
        for field, value in changes.items():
            o, st = opened()
            old = o.window["window_id"]
            o2, _ = make(st)
            base = o2.window_identity()
            with patch.object(so.ShadowObserver, "window_identity", lambda self, b=base: {**b, field: value}):
                with self.assertRaises(so.ObserverRefused, msg=field):
                    run(o2.open_window(DECISION_TS + M15))
            w = run(st.window(old))
            self.assertEqual(w["status"], es.W_INVALID, field)
            self.assertIn(field, w["invalidated_reason"]["identity_fields_changed"])
            with self.assertRaises(es.WindowRefused):
                run(st.put_candidate(old, record()))
            # no silent new window: a restart without explicit initialization is refused
            o3, _ = make(st)
            with self.assertRaises(so.ObserverRefused):
                run(o3.open_window(DECISION_TS + 2 * M15))
            self.assertEqual(len(run(st.windows())), 1)

    def test_real_code_sha_change_and_explicit_new_window(self):
        o, st = opened()
        old = o.window["window_id"]
        new = "9" * 40
        o2, _ = make(st, env={**ENV, "CANDIDATE_SHA": new}, runtime=observer_runtime(code_sha=new))
        with self.assertRaises(so.ObserverRefused):
            run(o2.open_window(DECISION_TS + M15))
        self.assertEqual(run(st.window(old))["status"], es.W_INVALID)
        o3, _ = make(st, env={**ENV, "CANDIDATE_SHA": new, "AI_EVIDENCE_NEW_WINDOW_AFTER": "wrong"},
                     runtime=observer_runtime(code_sha=new))
        with self.assertRaises(so.ObserverRefused):
            run(o3.open_window(DECISION_TS + M15))
        o4, _ = make(st, env={**ENV, "CANDIDATE_SHA": new, "AI_EVIDENCE_NEW_WINDOW_AFTER": old},
                     runtime=observer_runtime(code_sha=new))
        w = run(o4.open_window(DECISION_TS + M15))
        self.assertNotEqual(w["window_id"], old)
        self.assertEqual(w["identity"]["code_sha"], new)
        self.assertEqual(w["window_start_ms"], DECISION_TS + 2 * M15)

    def test_universe_order_is_pinned(self):
        r, _ = runtime_with_bundle("SHADOW")
        for syms in (list(reversed(fe.SHADOW_UNIVERSE)), list(fe.SHADOW_UNIVERSE)[:11],
                     list(fe.SHADOW_UNIVERSE) + ["BNBUSDT"]):
            with self.assertRaises(so.ObserverRefused):
                so.ShadowObserver({**ENV, "SHADOW_SYMBOLS": " ".join(syms)}, client=object(), runtime=r,
                                  manifest=rm.load(), store=mem())
        self.assertEqual(list(fe.SHADOW_UNIVERSE), ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
                                                    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT",
                                                    "NEARUSDT", "ATOMUSDT"])

    def test_one_active_window_per_db(self):
        st = mem()
        mk_window(st)
        with self.assertRaises(es.WindowRefused):
            mk_window(st, start=DECISION_TS + M15)


# ── continuity ──────────────────────────────────────────────────────────────
class Continuity(unittest.TestCase):
    def test_continuity_broken_survives_restart_and_is_never_cleared(self):
        o, st = opened()
        wid = o.window["window_id"]
        run(st.mark_broken(wid, "CANDIDATE_RECONSTRUCTION_MISMATCH"))
        with self.assertRaises(es.WindowRefused):
            run(st.update_window(wid, continuity_broken=False))
        o2, _ = make(st)                                               # restart: fresh in-process state
        with self.assertRaises(so.ObserverRefused):
            run(o2.open_window(DECISION_TS + M15))
        w = run(st.window(wid))
        self.assertTrue(w["continuity_broken"])
        self.assertEqual(w["continuity_reason"], "CANDIDATE_RECONSTRUCTION_MISMATCH")
        self.assertEqual(o2.status()["evidence_verdict"], "BLOCK")

    def test_db_outage_mid_boundary_then_restart_breaks_continuity(self):
        o, st = opened()
        wid = o.window["window_id"]

        def outage(symbol):
            if symbol == "XRPUSDT":
                st.fail = True
        wire(o, on_symbol=outage)
        with self.assertRaises(es.EvidenceContinuityBroken):
            run_boundary(o, DECISION_TS + 20_000)
        st.fail = False                                                # DB back; process died (no persist)
        o2, _ = make(st)
        with self.assertRaises(so.ObserverRefused):
            run(o2.open_window(DECISION_TS + M15))
        w = run(st.window(wid))
        self.assertTrue(w["continuity_broken"])
        self.assertTrue(w["continuity_reason"].startswith("INTERRUPTED_BOUNDARY"))

    def test_db_outage_persisted_once_db_returns(self):
        o, st = opened()
        wid = o.window["window_id"]
        st.fail = True
        with self.assertRaises(es.EvidenceContinuityBroken):
            run(st.put_candidate(wid, record()))
        with self.assertRaises(es.EvidenceContinuityBroken):
            run(o.persist_outage())                                    # DB still down
        st.fail = False
        self.assertTrue(run(o.persist_outage()))
        w = run(st.window(wid))
        self.assertTrue(w["continuity_broken"])
        self.assertTrue(w["continuity_reason"].startswith("DB_FAILURE"))
        o2, _ = make(st)
        with self.assertRaises(so.ObserverRefused):
            run(o2.open_window(DECISION_TS + M15))


# ── boundary journal / gaps ─────────────────────────────────────────────────
class BoundaryJournal(unittest.TestCase):
    def test_all_12_symbols_accounted_per_boundary(self):
        o, st = opened()
        wire(o)
        out = run_boundary(o, DECISION_TS + 20_000)
        self.assertEqual([r["symbol"] for r in out], list(fe.SHADOW_UNIVERSE))       # pinned order
        rows = run(st.boundaries(o.window["window_id"]))
        self.assertEqual(sorted(r["symbol"] for r in rows), sorted(fe.SHADOW_UNIVERSE))
        self.assertEqual({r["boundary_ms"] for r in rows}, {DECISION_TS})
        by = {r["symbol"]: r["status"] for r in rows}
        self.assertEqual(by[SYM], "AI_HOOK")
        self.assertEqual({v for k, v in by.items() if k != SYM}, {"NO_SIGNAL"})
        w = run(st.window(o.window["window_id"]))
        self.assertEqual((w["last_completed_boundary_ms"], w["completed_boundaries"], w["boundary_in_progress_ms"]),
                         (DECISION_TS, 1, None))
        # re-running the same boundary is a no-op (already accounted)
        self.assertEqual(run_boundary(o, DECISION_TS + 30_000)[0]["stage"], "BOUNDARY_ALREADY_ACCOUNTED")
        self.assertEqual(len(run(st.boundaries(o.window["window_id"]))), 12)
        self.assertEqual(o.orders_sent, 0)
        hb = run(st.heartbeats(o.window["window_id"]))
        self.assertEqual(hb[0]["safety"]["orders_sent"], 0)

    def test_missed_boundaries_detected_on_restart(self):
        o, st = opened()
        wid = o.window["window_id"]
        o2, _ = make(st)
        run(o2.open_window(DECISION_TS + 3 * M15 + 6 * 60_000))        # 4 boundaries passed unobserved
        rows = run(st.boundaries(wid))
        self.assertEqual(len(rows), 4 * 12)
        self.assertEqual({r["status"] for r in rows}, {"MISSED"})
        w = run(st.window(wid))
        self.assertEqual((w["missed_boundaries"], w["last_completed_boundary_ms"]), (4, DECISION_TS + 3 * M15))
        art = run(fa.build_forward_shadow_artifact_from_store(st))
        cov = art["coverage"]
        self.assertEqual(cov["by_status"], {"MISSED": 48})
        self.assertEqual(cov["complete_symbol_boundaries"], 0)
        self.assertEqual(cov["missing_symbol_boundaries"], 48)
        self.assertEqual(art["counts"]["candidates"], 0)             # a missed scan is not "zero candidates"
        self.assertEqual(cov["unaccounted_elapsed_symbol_boundaries"], 0)
        self.assertEqual(art["verdict"], "COLLECTING")

    def test_unprocessed_boundary_is_not_zero_candidates(self):
        o, st = opened()
        wid = o.window["window_id"]
        wire(o)
        run_boundary(o, DECISION_TS + 20_000)
        run(st.update_window(wid, last_completed_boundary_ms=DECISION_TS + M15))   # claimed but no journal rows
        art = run(fa.build_forward_shadow_artifact_from_store(st))
        self.assertEqual(art["coverage"]["unaccounted_elapsed_symbol_boundaries"], 12)
        self.assertFalse(art["completeness"]["satisfied"])
        self.assertEqual(art["FROZEN_POLICY_EVIDENCE_VALIDITY"]["status"], "INVALID_COVERAGE")
        self.assertFalse(art["FROZEN_POLICY_EVIDENCE_VALIDITY"]["valid"])

    def test_boundary_rows_are_unique_and_canonical(self):
        st = mem()
        wid = mk_window(st)
        self.assertEqual(run(st.put_boundary(wid, DECISION_TS, SYM, "NO_SIGNAL")), "INSERTED")
        self.assertEqual(run(st.put_boundary(wid, DECISION_TS, SYM, "NO_SIGNAL")), "IDEMPOTENT_DUPLICATE")
        for bad in ((DECISION_TS + 1, SYM, "NO_SIGNAL"), (DECISION_TS, "BNBUSDT", "NO_SIGNAL"),
                    (DECISION_TS - M15, SYM, "NO_SIGNAL"), (DECISION_TS + fe.WINDOW_DURATION_MS, SYM, "NO_SIGNAL")):
            with self.assertRaises(es.WindowRefused, msg=bad):
                run(st.put_boundary(wid, *bad))
        with self.assertRaises(ValueError):
            run(st.put_boundary(wid, DECISION_TS + M15, SYM, "PASS"))
        with self.assertRaises(es.EvidenceContinuityBroken):
            run(st.put_boundary(wid, DECISION_TS, SYM, "AI_HOOK"))  # conflicting reconstruction
        self.assertEqual(run(st.window(wid))["continuity_reason"], "BOUNDARY_RECONSTRUCTION_MISMATCH")


# ── decision candle / stale data ───────────────────────────────────────────
class DecisionCandle(unittest.TestCase):
    def test_missing_decision_candle_skips_evaluation(self):
        k15, k1h, k4h = market()
        o, st = opened()

        def boom(*a, **k):
            raise AssertionError("must not evaluate")
        with patch("bot.strategy.Analyzer.analyze_mtf", boom):
            r = run(o.evaluate(SYM, k15[:-1], k1h, k4h, decision_ts=DECISION_TS, now_ms=DECISION_TS + 60_000))
        self.assertEqual((r["stage"], r["reason"], r["detail"]),
                         ("DATA_MISSING", "BOUNDARY_INPUT_INCOMPLETE", "DECISION_CANDLE_MISSING"))
        self.assertEqual(so.boundary_status(r), "DATA_MISSING")
        self.assertEqual(st.t["ai_shadow_candidates"], {})

    def test_stale_inputs_are_data_missing(self):
        k15, k1h, k4h = market()
        forming = k15[-1]
        self.assertEqual(so.decision_inputs(k15[:-2] + [forming], k1h, k4h, DECISION_TS)["reason"], "STALE_15M")
        self.assertEqual(so.decision_inputs(k15, k1h[:-1], k4h, DECISION_TS)["reason"], "STALE_1H")
        self.assertEqual(so.decision_inputs(k15, k1h, k4h[:-1], DECISION_TS)["reason"], "STALE_4H")
        self.assertTrue(so.decision_inputs(k15, k1h, k4h, DECISION_TS)["ok"])

    def test_no_previous_close_fallback_and_exact_decision_open(self):
        src = inspect.getsource(so.ShadowObserver.evaluate)
        self.assertNotIn("w15[-1]", src)
        self.assertNotIn('"c"]', src)
        k15, k1h, k4h = market()
        seen = []
        from bot import nexus_oos_real_replay as rp
        orig = rp._decide

        def spy(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding, threshold):
            seen.append(ticker)
            return orig(nexus_ai, symbol, w15, w1h, w4h, sig, ticker, funding, threshold)
        o, st = opened()
        a, b = patched()
        with a, b, patch.object(rp, "_decide", spy):
            run(o.evaluate(SYM, k15, k1h, k4h, decision_ts=DECISION_TS, now_ms=DECISION_TS + 60_000))
        self.assertTrue(seen)
        self.assertEqual(float(seen[0]["lastPrice"]), float(k15[-1]["o"]))
        self.assertNotEqual(float(k15[-1]["o"]), float(k15[-1]["c"]))


# ── duplicates / binding / costs ────────────────────────────────────────────
class Duplicates(unittest.TestCase):
    def test_duplicate_is_idempotent_even_with_new_observation_time(self):
        st = mem()
        wid = mk_window(st)
        self.assertEqual(run(st.put_candidate(wid, record())), "INSERTED")
        self.assertEqual(run(st.put_candidate(wid, record(observed_at_ms=123, decision_latency_ms=9.0))),
                         "IDEMPOTENT_DUPLICATE")
        self.assertFalse(run(st.window(wid))["continuity_broken"])

    def test_conflicting_duplicate_breaks_continuity(self):
        st = mem()
        wid = mk_window(st)
        run(st.put_candidate(wid, record()))
        with self.assertRaises(es.EvidenceContinuityBroken):
            run(st.put_candidate(wid, record(entry=100.5)))
        w = run(st.window(wid))
        self.assertTrue(w["continuity_broken"])
        self.assertEqual(w["continuity_reason"], "CANDIDATE_RECONSTRUCTION_MISMATCH")
        with self.assertRaises(es.EvidenceContinuityBroken):
            run(st.put_candidate(wid, record(candidate_id="other")))    # collection halted
        art = run(fa.build_forward_shadow_artifact_from_store(st))
        self.assertEqual(art["verdict"], "BLOCK")
        self.assertIn("EVIDENCE_CONTINUITY_BROKEN", art["blockers"])

    def test_payload_hash_covers_decision_fields_only(self):
        base = record()
        h = es.candidate_payload_sha256(base)
        for k in es.NON_PAYLOAD_FIELDS:
            self.assertEqual(es.candidate_payload_sha256({**base, k: "x"}), h, k)
        for k in ("entry", "signal_stop", "final_tp", "ai_decision", "probability_calibrated", "predicted_net_r",
                  "assumptions", "direction", "bundle_sha256"):
            self.assertNotEqual(es.candidate_payload_sha256({**base, k: "changed"}), h, k)


class Binding(unittest.TestCase):
    def test_candidate_and_heartbeat_window_binding(self):
        st = mem()
        w1 = mk_window(st)
        run(st.put_candidate(w1, record()))
        run(st.update_window(w1, status=es.W_CLOSED, closed_at_ms=DECISION_TS + fe.WINDOW_DURATION_MS))
        w2 = mk_window(st, start=DECISION_TS + fe.WINDOW_DURATION_MS)
        self.assertEqual(run(st.candidates(w2)), [])                 # no mixing across windows
        with self.assertRaises(es.WindowRefused):
            run(st.put_candidate(w1, record(candidate_id="late")))   # closed window refuses appends
        with self.assertRaises(es.WindowRefused):
            run(st.heartbeat("nope", {"ts": 1, "kind": "SCAN"}))
        with self.assertRaises(es.WindowRefused):
            run(st.put_candidate(w2, record(candidate_id="x", event_ts=DECISION_TS + fe.WINDOW_DURATION_MS,
                                            bundle_sha256="z" * 64)))
        run(st.heartbeat(w2, {"ts": DECISION_TS + fe.WINDOW_DURATION_MS, "kind": "SCAN"}))
        self.assertEqual(run(st.heartbeats(w1)), [])
        # a row re-pointed to another window fails verification
        w, s_, k, raw = st.t["ai_shadow_candidates"]["cand-1"]
        st.t["ai_shadow_candidates"]["cand-1"] = (w2, s_, k, raw)
        with self.assertRaises(JournalIntegrityError):
            run(st.candidates(w2))

    def test_trades_table_with_zero_rows_is_refused(self):
        with self.assertRaises(es.EvidenceStoreRefused):
            run(es.MemoryEvidenceStore(allow_non_durable_for_tests=True, trades_table=True).init())

    def test_costs_cannot_change_mid_window(self):
        st = mem()
        wid = mk_window(st)
        with self.assertRaises(es.EvidenceContinuityBroken):
            run(st.put_candidate(wid, record(assumptions={"fee_rate": 0.001, "slippage_rate": 0.0005})))
        self.assertEqual(run(st.window(wid))["continuity_reason"], "COST_IDENTITY_MISMATCH")
        # a changed fee env on restart fails replay-manifest runtime parity: startup refused,
        # nothing can be appended to (or created next to) the existing window
        o, st2 = opened()
        with patch.dict(os.environ, {"TAKER_FEE": "0.0009"}):
            with self.assertRaises(so.ObserverRefused):
                make(st2)
        self.assertEqual(len(run(st2.windows())), 1)
        self.assertEqual(run(st2.candidates(o.window["window_id"])), [])

    def test_candidate_inherits_the_window_cost_identity(self):
        o, st = opened()
        observe(o)
        rec = run(st.candidates(o.window["window_id"]))[0]
        cost = o.window["identity"]["cost_identity"]
        self.assertEqual(rec["assumptions"]["fee_rate"], cost["taker_fee"])
        self.assertEqual(rec["assumptions"]["slippage_rate"], cost["slippage_rates"][SYM])
        for k in ("taker_fee", "slippage_model_version", "slippage_rates", "exit_policy_sha256",
                  "replay_policy_manifest_sha256"):
            self.assertIn(k, cost)


# ── window end ──────────────────────────────────────────────────────────────
class WindowEnd(unittest.TestCase):
    def test_no_candidates_after_end_and_pending_censored_at_fixed_end(self):
        st = mem()
        wid = mk_window(st)
        end = DECISION_TS + fe.WINDOW_DURATION_MS
        with self.assertRaises(es.WindowRefused):
            run(st.put_candidate(wid, record(candidate_id="after", event_ts=end)))
        ev = end - 2 * M15
        run(st.put_candidate(wid, record(candidate_id="late", event_ts=ev)))
        tp_after_end = bars_from(ev, [100.0, 100.05, 100.1, 107.0, 107.0])   # TP only after window end

        async def fb(*a):
            return tp_after_end

        async def ff(*a):
            return []
        with self.assertRaises(es.WindowRefused):                        # no early stopping
            run(fc.close_window(st, wid, fb, ff, exit_policy=EXIT, now_ms=end - 1))
        res = run(fc.close_window(st, wid, fb, ff, exit_policy=EXIT, now_ms=end + 10 * M15))
        self.assertEqual(res["event"], "FORWARD_WINDOW_CLOSED")
        self.assertEqual(res["resolver"]["information_horizon_ms"], end)
        rec = run(st.get("late"))
        self.assertEqual(rec["status"], "RIGHT_CENSORED_DATA_END")
        w = run(st.window(wid))
        self.assertEqual((w["status"], w["closed_at_ms"]), ("CLOSED", end))
        self.assertTrue(run(fc.close_window(st, wid, fb, ff, exit_policy=EXIT, now_ms=end + 99 * M15))
                        ["already_closed"])

    def test_observer_closes_at_end_and_never_starts_another_window(self):
        o, st = opened()
        wid = o.window["window_id"]
        end = o.window["window_end_ms"]
        run(st.update_window(wid, last_completed_boundary_ms=end - M15))    # (as if observed throughout)
        wire(o)
        out = run_boundary(o, end + 20_000)
        self.assertEqual(out[0]["event"], "FORWARD_WINDOW_CLOSED")
        self.assertTrue(o.closed)
        self.assertEqual(run_boundary(o, end + M15), [])
        self.assertEqual(o.status()["evidence_verdict"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(run(st.window(wid))["status"], "CLOSED")
        o2, _ = make(st)
        with self.assertRaises(so.ObserverRefused):
            run(o2.open_window(end + M15))
        self.assertEqual(len(run(st.windows())), 1)


# ── artifact from the store ─────────────────────────────────────────────────
class ArtifactFromStore(unittest.TestCase):
    def _collected(self):
        o, st = opened()
        wire(o)
        run_boundary(o, DECISION_TS + 20_000)
        return o, st

    def test_artifact_derives_window_and_identity_from_db(self):
        o, st = self._collected()
        art = run(fa.build_forward_shadow_artifact_from_store(st))
        w = run(st.window(o.window["window_id"]))
        self.assertEqual(art["window_id"], w["window_id"])
        self.assertEqual((art["window"]["start_ms"], art["window"]["end_ms"]), (w["window_start_ms"], w["window_end_ms"]))
        self.assertEqual(art["window"]["fixed_duration_ms"], fe.WINDOW_DURATION_MS)
        self.assertEqual(art["window"]["start_source"], "DURABLE_WINDOW_ROW")
        self.assertEqual(art["window_identity"], w["identity"])
        self.assertEqual(art["contract"], "FORWARD_SHADOW_EVIDENCE_V3")
        self.assertEqual(art["symbols"], list(fe.SHADOW_UNIVERSE))
        self.assertTrue(art["journal_verified"])
        self.assertEqual(art["journal_verified_source"], "COMPUTED_FROM_STORE")
        self.assertEqual(art["verdict"], "COLLECTING")
        self.assertTrue(art["zero_order_verified"])
        self.assertEqual(art["counts"]["candidates"], 1)
        self.assertEqual(run(fa.build_forward_shadow_artifact_from_store(st)), art)         # reproducible
        self.assertNotIn("://", fa.sanitized_json(art))

    def test_tampering_is_detected_and_journal_verified_cannot_be_faked(self):
        for table in ("ai_shadow_boundaries", "ai_shadow_heartbeats", "ai_shadow_candidates"):
            o, st = self._collected()
            pk = sorted(st.t[table])[0]
            w, s_, k, raw = st.t[table][pk]
            d = json.loads(raw)
            d["tampered"] = True
            st.t[table][pk] = (w, s_, k, json.dumps(d))
            art = run(fa.build_forward_shadow_artifact_from_store(st))
            self.assertFalse(art["journal_verified"], table)
            self.assertEqual(art["verdict"], "BLOCK")
            self.assertIn("JOURNAL_NOT_VERIFIED", art["blockers"])
        with self.assertRaises(TypeError):
            run(fa.build_forward_shadow_artifact_from_store(st, journal_verified=True))

    def test_coverage_metrics(self):
        st = mem()
        wid = mk_window(st)
        statuses = ["COMPLETE", "NO_SIGNAL", "AI_HOOK", "NEXUS_REJECTED", "DATA_MISSING", "ERROR"] * 2
        for b in range(2):
            for sym, stt in zip(fe.SHADOW_UNIVERSE, statuses):
                run(st.put_boundary(wid, DECISION_TS + b * M15, sym, stt))
        run(st.update_window(wid, last_completed_boundary_ms=DECISION_TS + M15, completed_boundaries=2))
        cov = run(fa.build_forward_shadow_artifact_from_store(st))["coverage"]
        self.assertEqual(cov["expected_boundaries"], 2880)
        self.assertEqual(cov["expected_symbol_boundaries"], 2880 * 12)
        self.assertEqual(cov["elapsed_boundaries"], 2)
        self.assertEqual(cov["journaled_symbol_boundaries"], 24)
        self.assertEqual(cov["complete_symbol_boundaries"], 16)
        self.assertEqual(cov["missing_symbol_boundaries"], 4)
        self.assertEqual(cov["error_symbol_boundaries"], 4)
        self.assertAlmostEqual(cov["coverage_fraction"], 16 / 34560)
        self.assertAlmostEqual(cov["coverage_fraction_elapsed"], 16 / 24)
        self.assertEqual(cov["unaccounted_symbol_boundaries"], 34560 - 24)

    def test_coverage_failure_disqualifies_policy_evidence(self):
        st = mem()
        wid = mk_window(st)
        for sym in fe.SHADOW_UNIVERSE:
            run(st.put_boundary(wid, DECISION_TS, sym, "NO_SIGNAL"))
        run(st.put_candidate(wid, record(ai_decision="TRADE")))
        run(st.finalize("cand-1", "RESOLVED", {"modeled_net_r": 1.0, "outcome_end_ts": DECISION_TS + M15}))
        run(st.update_window(wid, status=es.W_CLOSED, closed_at_ms=DECISION_TS + fe.WINDOW_DURATION_MS,
                             last_completed_boundary_ms=DECISION_TS))
        art = run(fa.build_forward_shadow_artifact_from_store(st))
        self.assertFalse(art["completeness"]["satisfied"])
        self.assertIn("PROCESSED_COVERAGE_BELOW_RULE", art["completeness"]["failures"])
        self.assertEqual(art["FROZEN_POLICY_EVIDENCE_VALIDITY"]["status"], "INVALID_COVERAGE")
        self.assertTrue(art["FROZEN_POLICY_FORWARD_EVIDENCE"]["status"].startswith("DISQUALIFIED_"))
        self.assertEqual(art["FROZEN_POLICY_FORWARD_EVIDENCE"]["authority"], {})
        self.assertIn("COMPLETENESS_RULE_FAILED", art["blockers"])
        self.assertEqual(art["verdict"], "BLOCK")
        self.assertTrue(art["OBSERVATION_DATASET_VALIDITY"]["valid"])          # the dataset stays usable

    def test_observer_and_artifact_never_report_pass(self):
        o, st = self._collected()
        self.assertIn(o.status()["evidence_verdict"], ("COLLECTING", "INSUFFICIENT_EVIDENCE", "BLOCK"))
        self.assertEqual(fa.IN_CANDIDATE_VERDICTS, ("COLLECTING", "INSUFFICIENT_EVIDENCE", "BLOCK"))


# ── contract ────────────────────────────────────────────────────────────────
class ContractV3(unittest.TestCase):
    def test_v3_fixed_window_and_superseded_contracts(self):
        c = fe.SHADOW_CONTRACT
        self.assertEqual(c["name"], "FORWARD_SHADOW_EVIDENCE_V3")
        self.assertEqual(c["window"]["duration_ms"], 30 * 86_400_000)
        self.assertEqual((c["window"]["extension"], c["window"]["optional_stopping"]), ("FORBIDDEN", "FORBIDDEN"))
        self.assertFalse(c["window"]["auto_new_window"])
        self.assertEqual(c["boundary_journal"]["expected_symbol_boundaries"], 2880 * 12)
        self.assertEqual(c["symbol_universe"], list(fe.SHADOW_UNIVERSE))
        self.assertEqual(c["symbol_universe_sha256"], fe.universe_sha256(fe.SHADOW_UNIVERSE))
        for name in ("FORWARD_SHADOW_EVIDENCE_V1", "FORWARD_SHADOW_EVIDENCE_V2"):
            self.assertEqual(fe.SUPERSEDED[name]["status"], "SUPERSEDED_BEFORE_FIRST_FORWARD_WINDOW")
            self.assertEqual(fe.SUPERSEDED[name]["forward_windows_run"], 0)
        self.assertEqual(fe.CONTRACT_SHA256["SHADOW"], fe._sha(c))
        self.assertEqual(fe.COMPLETENESS_RULE["min_processed_symbol_boundary_fraction"], 0.99)

    def test_committed_wheel_removed(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(glob.glob(os.path.join(root, "*.whl")), [])


# ── real PostgreSQL ─────────────────────────────────────────────────────────
class RealPostgresWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = _pg_cluster()
        if cls.pg is None:
            raise unittest.SkipTest("no local PostgreSQL binaries")

    @classmethod
    def tearDownClass(cls):
        cls.pg["stop"]()

    def _db(self, name):
        async def mk():
            import asyncpg
            c = await asyncpg.connect(self.pg["url"])
            await c.execute(f"CREATE DATABASE {name}")
            await c.close()
        run(mk())
        return {**GOOD, "EVIDENCE_DATABASE_URL": self.pg["url"].rsplit("/", 1)[0] + "/" + name}

    def test_window_lifecycle_uniqueness_and_export(self):
        env = self._db("win7e")

        async def go():
            st = await es.EvidenceStore.connect(env)
            ident = fixture_identity()
            w = await st.create_window(ident, created_at_ms=DECISION_TS - 1, first_boundary_ms=DECISION_TS,
                                       window_end_ms=DECISION_TS + fe.WINDOW_DURATION_MS,
                                       expected_boundaries=fe.EXPECTED_BOUNDARIES)
            wid = w["window_id"]
            with self.assertRaises(Exception):                           # DB-level one-ACTIVE index
                await st._ins("ai_evidence_windows", "x", "x", es.W_ACTIVE, 0, "{}")
            with self.assertRaises(Exception):                           # DB-level FK
                await st._ins("ai_shadow_candidates", "c", "no-window", "PENDING", 0, "{}")
            self.assertEqual(await st.put_candidate(wid, record()), "INSERTED")
            self.assertEqual(await st.put_candidate(wid, record(observed_at_ms=5)), "IDEMPOTENT_DUPLICATE")
            for sym in fe.SHADOW_UNIVERSE:
                await st.put_boundary(wid, DECISION_TS, sym, "NO_SIGNAL")
            self.assertEqual(await st.put_boundary(wid, DECISION_TS, SYM, "NO_SIGNAL"), "IDEMPOTENT_DUPLICATE")
            await st.update_window(wid, last_completed_boundary_ms=DECISION_TS, completed_boundaries=1)
            await st.heartbeat(wid, {"ts": DECISION_TS, "kind": "SCAN", "safety": {
                "exchange_credentials_present": False, "mutating_client_methods": False,
                "execution_lease_acquired": False, "orders_sent": 0}})
            with self.assertRaises(es.EvidenceContinuityBroken):
                await st.put_candidate(wid, record(entry=101.0))
            await st.conn.close()
            # restart: continuity_broken is durable
            st2 = await es.EvidenceStore.connect(env)
            w2 = await st2.active_window()
            self.assertEqual(w2["window_id"], wid)
            self.assertTrue(w2["continuity_broken"])
            with self.assertRaises(es.WindowRefused):
                await st2.update_window(wid, continuity_broken=False)
            await st2.conn.close()
            out1 = os.path.join(tempfile.mkdtemp(), "a.json")
            out2 = os.path.join(tempfile.mkdtemp(), "b.json")
            art = await fa._export(env, out1)
            await fa._export(env, out2)
            return art, open(out1).read(), open(out2).read()
        art, t1, t2 = run(go())
        self.assertEqual(t1, t2)                                           # reproducible export
        self.assertNotIn("://", t1)
        self.assertNotIn("postgresql", t1.lower().replace("postgresql (bot.ai", ""))
        self.assertEqual(art["verdict"], "BLOCK")
        self.assertTrue(art["continuity"]["continuity_broken"])
        self.assertEqual(art["coverage"]["complete_symbol_boundaries"], 12)

    def test_trades_table_with_zero_rows_refused_and_readonly_export_writes_nothing(self):
        env = self._db("prod7e")
        env2 = self._db("empty7e")

        async def go():
            import asyncpg
            c = await asyncpg.connect(env["EVIDENCE_DATABASE_URL"])
            await c.execute("CREATE TABLE trades (id INT)")               # zero rows
            await c.close()
            with self.assertRaises(es.EvidenceStoreRefused):
                await es.EvidenceStore.connect(env)
            with self.assertRaises(es.EvidenceStoreRefused):              # read-only: no role marker
                await es.EvidenceStore.connect(env2, read_only=True)
            c2 = await asyncpg.connect(env2["EVIDENCE_DATABASE_URL"])
            n = await c2.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            await c2.close()
            return n
        self.assertEqual(run(go()), 0)


# ── Phase 7E final: runtime provenance lock ─────────────────────────────────
class RuntimeProvenance(unittest.TestCase):
    def refused(self, *, env=None, runtime=None, contains=None):
        st = mem()
        with self.assertRaises(so.ObserverRefused) as cm:
            make(st, env=env, runtime=runtime)
        self.assertEqual(st.t["ai_evidence_windows"], {})                 # no window
        self.assertEqual(st.t["ai_shadow_candidates"], {})                # no observation
        if contains:
            self.assertIn(contains, str(cm.exception))
        return cm.exception

    # manifest runtime parity
    def test_verify_runtime_runs_before_window_creation(self):
        order = []
        orig_v, orig_c = rm.ReplayManifest.verify_runtime, es.EvidenceStore.create_window

        def v(self_):
            order.append("verify_runtime")
            return orig_v(self_)

        async def c(self_, *a, **k):
            order.append("create_window")
            return await orig_c(self_, *a, **k)
        with patch.object(rm.ReplayManifest, "verify_runtime", v), patch.object(es.EvidenceStore, "create_window", c):
            o, st = opened()
        self.assertEqual(order, ["verify_runtime", "create_window"])
        self.assertLess(o.steps.index("REPLAY_MANIFEST_RUNTIME_PARITY"), o.steps.index("WINDOW_COMMITTED"))
        with patch.object(rm.ReplayManifest, "verify_runtime", side_effect=rm.ManifestError("MISMATCH")):
            self.refused(contains="RUNTIME_MANIFEST_PARITY_FAILED")

    def test_taker_fee_and_slippage_mismatch_refuse(self):
        with patch.dict(os.environ, {"TAKER_FEE": "0.0009"}):
            self.refused(contains="RUNTIME_MANIFEST_PARITY_FAILED")
        with patch.dict(os.environ, {"BACKTEST_SLIPPAGE": "0.001"}):
            self.refused(contains="RUNTIME_MANIFEST_PARITY_FAILED")

    def test_min_volume_mult_and_nexus_threshold_mismatch_refuse(self):
        from bot import nexus_ai
        from bot.config import cfg
        with patch.object(cfg, "MIN_VOLUME_MULT", float(cfg.MIN_VOLUME_MULT) + 0.1):
            self.refused(contains="RUNTIME_MANIFEST_PARITY_FAILED")
        with patch.object(nexus_ai, "MIN_SCORE", float(nexus_ai.MIN_SCORE) + 1.0):
            self.refused(contains="RUNTIME_MANIFEST_PARITY_FAILED")

    def test_valid_parity_permits_startup_with_explicit_cost_parity(self):
        from bot.kucoin_execution_model import configured_taker_fee, slippage_rate_for_symbol
        o, st = make()
        man = rm.load()
        self.assertEqual(o.runtime_parity["status"], "PASS")
        for k in ("TAKER_FEE", "BACKTEST_SLIPPAGE", "MIN_VOLUME_MULT", "NEXUS_MIN_SCORE_EFFECTIVE", "MIN_ENTRY_SCORE",
                  "FEE_MULTIPLIER", "TRAILING_TRIGGER", "TRAILING_LOCK", "NEXUS_MAX_SIGNAL_DRIFT_BPS"):
            self.assertIn(k, o.runtime_parity["verified_keys"])
        self.assertEqual(configured_taker_fee(), man["TAKER_FEE"])
        self.assertEqual(slippage_rate_for_symbol("BTCUSDT"), man["BACKTEST_SLIPPAGE"])
        self.assertEqual(slippage_rate_for_symbol("ATOMUSDT"), 2 * man["BACKTEST_SLIPPAGE"])   # x2 non-major rule

    # code sha
    def test_code_sha_must_be_exact_40_lower_hex(self):
        for bad in (None, "", "c" * 39, "c" * 41, "g" * 40, "C" * 40, "main", "claude/nexus7-production"):
            env = {k: v for k, v in ENV.items() if k != "CANDIDATE_SHA"}
            if bad is not None:
                env["CANDIDATE_SHA"] = bad
            self.refused(env=env, contains="40 lowercase hex")

    def test_railway_sha_is_authority_and_disagreement_refuses(self):
        a = "a" * 40
        self.assertEqual(so.resolve_code_sha({"RAILWAY_GIT_COMMIT_SHA": a}), a)
        self.assertEqual(so.resolve_code_sha({"RAILWAY_GIT_COMMIT_SHA": a, "CANDIDATE_SHA": a}), a)
        self.assertEqual(so.resolve_code_sha({"CANDIDATE_SHA": a}), a)               # offline only
        env = {k: v for k, v in ENV.items() if k != "CANDIDATE_SHA"}
        o, _ = make(env={**env, "RAILWAY_GIT_COMMIT_SHA": a}, runtime=observer_runtime(code_sha=a))
        self.assertEqual(o.code_sha, a)
        self.refused(env={**ENV, "RAILWAY_GIT_COMMIT_SHA": a}, runtime=observer_runtime(code_sha=a),
                     contains="disagree")

    def test_code_sha_must_match_bundle_and_metadata(self):
        self.refused(runtime=observer_runtime(code_sha="d" * 40, meta_over={"candidate_code_sha": "c" * 40}),
                     contains="CODE_BUNDLE_SHA_MISMATCH")                   # bundle training_code_sha differs
        self.refused(runtime=observer_runtime(code_sha="c" * 40, meta_over={"candidate_code_sha": "e" * 40}),
                     contains="CODE_BUNDLE_SHA_MISMATCH")                   # metadata candidate sha differs
        self.refused(runtime=observer_runtime(code_sha="d" * 40), contains="CODE_BUNDLE_SHA_MISMATCH")

    # bundle metadata
    def test_bundle_metadata_must_agree_with_loaded_bundle(self):
        for over in ({"bundle_sha256": "0" * 64}, {"policy_sha256": "0" * 64}, {"feature_schema_sha256": "0" * 64},
                     {"hook_profile": "PAPER_OR_UNPILOTED_PRE_GEOMETRY"}, {"hook_population": "X"},
                     {"dataset_manifest_sha256": "0" * 64}, {"lifecycle_state": "SHADOW_CHALLENGER"},
                     {"schema": "UNKNOWN"}, {"candidate_code_sha": None}):
            self.refused(runtime=observer_runtime(meta_over=over), contains="BUNDLE_METADATA_INVALID")
        self.refused(runtime=observer_runtime(metadata=False), contains="BUNDLE_METADATA_INVALID")

    def test_lifecycle_must_be_shadow_safe(self):
        for bad in ("RESEARCH_CANDIDATE", "PAPER_CHALLENGER", "LIVE_CHAMPION"):
            self.refused(runtime=observer_runtime(lifecycle=bad), contains="refused by the isolated observer")
        for ok in ("SHADOW_OBSERVER", "SHADOW_CHALLENGER"):
            o, _ = make(runtime=observer_runtime(lifecycle=ok))
            self.assertIn("CODE_PROVENANCE_VERIFIED", o.steps)
        self.assertEqual(o.bundle_metadata["claims"],
                         {"edge_claim": False, "order_authority": False, "live_authority": False})

    # deploy pins
    def test_replay_policy_and_contract_pins(self):
        for key in ("SHADOW_REPLAY_POLICY_SHA256", "SHADOW_FORWARD_CONTRACT_SHA256"):
            self.refused(env={**ENV, key: "0" * 64})
            self.refused(env={k: v for k, v in ENV.items() if k != key}, contains="deploy-pinned")
        self.refused(env={**ENV, "SHADOW_REPLAY_POLICY_SHA256": "0" * 64}, contains="REPLAY_POLICY_SHA_MISMATCH")
        self.refused(env={**ENV, "SHADOW_FORWARD_CONTRACT_SHA256": "0" * 64}, contains="FORWARD_CONTRACT_SHA_MISMATCH")

    def test_window_identity_contains_all_pinned_provenance(self):
        o, st = opened()
        ident = run(st.window(o.window["window_id"]))["identity"]
        man = o.runtime.bundle.manifest
        expect = {"code_sha": "c" * 40, "bundle_sha256": man["bundle_sha256"],
                  "policy_sha256": man["decision_policy_sha256"], "feature_schema_sha256": man["feature_schema_sha256"],
                  "training_dataset_manifest_sha256": man["training_dataset_manifest_sha256"],
                  "replay_policy_manifest_sha256": rm.load().sha256,
                  "forward_contract_sha256": fe.CONTRACT_SHA256["SHADOW"],
                  "runtime_manifest_parity_status": "PASS", "hook_population": man["hook_population"],
                  "hook_profile": man["hook_profile"], "symbol_universe": list(fe.SHADOW_UNIVERSE),
                  "evidence_db_authority_id": "test-memory", "evidence_db_fingerprint": "memory",
                  "bundle_lifecycle_state": "SHADOW_OBSERVER"}
        for k, v in expect.items():
            self.assertEqual(ident[k], v, k)
        self.assertIn("TAKER_FEE", ident["replay_runtime_verified_keys"])
        self.assertIn("cost_identity", ident)
        self.assertNotIn("://", json.dumps(ident))

    def test_decision_and_observation_carry_the_verified_code_sha(self):
        o, st = opened()
        with self.assertRaises(so.ObserverRefused):
            make_unstarted = so.ShadowObserver(ENV, client=object(), runtime=observer_runtime(), manifest=rm.load(),
                                               store=mem())
            make_unstarted.observation_line()
        observe(o)
        rec = run(st.candidates(o.window["window_id"]))[0]
        sha = o.window["identity"]["code_sha"]
        self.assertEqual(sha, "c" * 40)
        self.assertEqual(rec["decision_candidate_sha"], sha)
        self.assertEqual(rec["code_sha"], sha)
        self.assertEqual(o.runtime.bundle.manifest["training_code_sha"], sha)
        self.assertEqual(o.bundle_metadata["candidate_code_sha"], sha)
        self.assertEqual(o.runtime.authority.candidate_sha, sha)
        line = o.observation_line(deployment_id="dep-1")
        self.assertIn(f"candidate_sha={sha}", line)
        self.assertNotIn("UNAVAILABLE", line.split("candidate_sha=")[1].split()[0])
        with self.assertRaises(es.WindowRefused):                          # decision sha must equal window sha
            run(st.put_candidate(o.window["window_id"], {**rec, "candidate_id": "x", "decision_candidate_sha": None}))


class ZeroOrderObservation(unittest.TestCase):
    SAFE = {"exchange_credentials_present": False, "mutating_client_methods": False,
            "execution_lease_acquired": False, "orders_sent": 0}

    def test_no_scan_heartbeat_is_not_verified(self):
        o, st = opened()
        art = run(fa.build_forward_shadow_artifact_from_store(st))
        self.assertEqual(art["zero_order_status"], "NOT_YET_OBSERVED")
        self.assertFalse(art["zero_order_verified"])

    def test_safe_completed_boundary_with_scan_is_verified(self):
        o, st = opened()
        wire(o)
        run_boundary(o, DECISION_TS + 20_000)
        art = run(fa.build_forward_shadow_artifact_from_store(st))
        self.assertEqual(art["zero_order_status"], "VERIFIED")
        self.assertTrue(art["zero_order_verified"])
        cov = art["scan_safety_coverage"]
        self.assertEqual((cov["completed_boundaries"], cov["safe_scan_heartbeats"], cov["coverage_fraction"]),
                         (1, 1, 1.0))

    def test_completed_boundary_without_scan_is_coverage_failure(self):
        st = mem()
        wid = mk_window(st)
        for sym in fe.SHADOW_UNIVERSE:
            run(st.put_boundary(wid, DECISION_TS, sym, "NO_SIGNAL"))
            run(st.put_boundary(wid, DECISION_TS + M15, sym, "NO_SIGNAL"))
        run(st.heartbeat(wid, {"ts": DECISION_TS, "kind": "SCAN", "boundary_ms": DECISION_TS, "safety": self.SAFE}))
        run(st.update_window(wid, last_completed_boundary_ms=DECISION_TS + M15, completed_boundaries=2))
        art = run(fa.build_forward_shadow_artifact_from_store(st))
        self.assertEqual(art["zero_order_status"], "INCOMPLETE")
        self.assertFalse(art["zero_order_verified"])
        self.assertEqual(art["scan_safety_coverage"]["completed_boundaries_without_safe_scan"], 1)
        self.assertIn("SCAN_SAFETY_COVERAGE_INCOMPLETE", art["completeness"]["failures"])
        self.assertFalse(art["FROZEN_POLICY_EVIDENCE_VALIDITY"]["valid"])

    def test_unsafe_heartbeat_blocks(self):
        for bad in ({"orders_sent": 1}, {"exchange_credentials_present": True}, {"mutating_client_methods": True},
                    {"execution_lease_acquired": True}):
            st = mem()
            wid = mk_window(st)
            for sym in fe.SHADOW_UNIVERSE:
                run(st.put_boundary(wid, DECISION_TS, sym, "NO_SIGNAL"))
            run(st.heartbeat(wid, {"ts": DECISION_TS, "kind": "SCAN", "boundary_ms": DECISION_TS,
                                   "safety": {**self.SAFE, **bad}}))
            art = run(fa.build_forward_shadow_artifact_from_store(st))
            self.assertEqual(art["zero_order_status"], "VIOLATION", bad)
            self.assertEqual(art["verdict"], "BLOCK")
            self.assertIn("ZERO_ORDER_ASSERTION_FAILED", art["blockers"])


class DeployProvenance(unittest.TestCase):
    def test_deploy_manifest_pins_replay_policy_and_contract(self):
        from bot.ai import bundle_export as bx
        from tests.test_ai_phase7d import ShadowBundleArtifact
        art = ShadowBundleArtifact._artifact(None)
        tmp = tempfile.mkdtemp()
        res = bx.export(art, tmp, candidate_sha="c" * 40)
        dm = bx.deploy_manifest(res)
        self.assertEqual(dm["replay_policy_manifest_sha256"], rm.load().sha256)
        self.assertEqual(dm["pinned_env"]["SHADOW_REPLAY_POLICY_SHA256"], rm.load().sha256)
        self.assertEqual(dm["pinned_env"]["SHADOW_FORWARD_CONTRACT_SHA256"], fe.CONTRACT_SHA256["SHADOW"])
        self.assertTrue(dm["provenance"]["mutually_consistent"])
        self.assertEqual(dm["provenance"]["candidate_code_sha"], dm["provenance"]["training_code_sha"])
        v = bx.verify(tmp)
        self.assertTrue(v["metadata_verified"])
        self.assertEqual(v["candidate_code_sha"], "c" * 40)
        with self.assertRaises(bx.BundleExportError):
            bx.export(art, tempfile.mkdtemp(), candidate_sha="d" * 40)          # CODE_BUNDLE_SHA_MISMATCH


if __name__ == "__main__":
    unittest.main()
