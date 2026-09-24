"""Phase 7C: runtime hook population parity, aggregate bootstrap verifier,
AI_LIVE Stage-C consistency, journal integrity/transitions, full-policy
selection stability, and the isolated SHADOW observer.
"""
import asyncio
import copy
import datetime as dt
import inspect
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import nexus_oos_execution_parity as xp
from bot import nexus_oos_promotion_gate as gate
from bot.ai import ai_gate
from bot.ai import decision as dec
from bot.ai import features as fx
from bot.ai import hook as hk
from bot.ai import identity as aid
from bot.ai import lifecycle as lc
from bot.ai import runtime as rt
from bot.ai import shadow_observer as so
from bot.ai import training as tr
from bot.config import cfg
from bot.nexus_types import NexusDecision
from tests.test_ai_decision_authority import DECISION_TS, H1, H4, M15, candles, constant_bundle
from tests.test_ai_phase7b import FakeDB, _ai, passing_artifact, run, runtime_with_bundle, synthetic_rows
from tests import test_live_release_evidence as tlre

SYM, KSYM = "SOLUSDT", "SOLUSDTM"
MMR = 0.02


def market():
    k15 = candles(220, M15, DECISION_TS)
    k1h = candles(120, H1, DECISION_TS, seed=2)
    k4h = candles(130, H4, DECISION_TS, seed=3)
    forming = {"ts": DECISION_TS, "o": k15[-1]["c"], "h": k15[-1]["c"] * 1.01, "l": k15[-1]["c"] * 0.99,
               "c": k15[-1]["c"] * 1.005, "v": 1.0}
    return k15 + [forming], k1h, k4h


def setUpModule():
    # Production installs the runtime overlays (incl. the CROSS geometry
    # wrapper of TradingEngine._nexus_validate) before trading; so does the replay.
    from bot.runtime_bootstrap import install
    install()


def signal(sl=90.0, tp=130.0):
    from bot.strategy import Signal
    return Signal(symbol=SYM, direction="LONG", entry=100.0, sl=sl, tp=tp, confidence=70.0,
                  score=78, regime="RANGING", expected_pnl=1.0, total_fees=0.1)


def fake_decide(*, reject_initial=False, reject_recheck=False):
    """Deterministic NEXUS stand-in: confidence depends on the stop, so the
    hook must carry the decision of the geometry it actually saw."""
    def decide(*args, entry, sl, tp, symbol=None, **kw):
        symbol = symbol or args[0]
        recheck = abs(sl - 90.0) > 1e-9
        allowed = not (reject_recheck if recheck else reject_initial)
        return NexusDecision(symbol=symbol, decision="LONG", confidence=50.0 + 100 * abs(entry - sl) / entry,
                             setup_quality=70.0, data_quality=100.0, entry=entry, stop_loss=sl, take_profit=tp,
                             expected_value=0.5, risk_reward=abs(tp - entry) / abs(entry - sl), reasoning=[],
                             execution_allowed=allowed)
    return decide


class _EngineClient:
    """Read-only stub for the real engine pre-hook path (klines + CROSS risk reads)."""

    def __init__(self, k15, k1h, k4h):
        self.k = {"15": k15, "60": k1h, "240": k4h}

    def get_cached_klines(self, sym, iv, limit):
        return list(self.k[iv])

    async def get_klines(self, *a, **k):
        return []

    def get_cached_ticker(self, sym):
        return None

    async def get_funding_rate(self, sym):
        return None

    async def get_open_interest(self, sym):
        return None

    async def _get(self, path, params=None, auth=False):
        if path == "/api/v1/account-overview":
            return {"marginBalance": 1000.0}
        if path == "/api/v2/batchGetCrossOrderLimit":
            return [{"symbol": KSYM, "mmr": MMR, "leverage": float(cfg.LEVERAGE)}]
        raise AssertionError(path)


def engine_hook(profile, decide, market_data):
    """Real TradingEngine path: scan score adjustment -> wrapped _nexus_validate
    (CROSS geometry + recheck in the LIVE pilot profile) -> _ai_gate."""
    from bot import engine as eng
    k15, k1h, k4h = market_data
    e = eng.TradingEngine.__new__(eng.TradingEngine)
    e.client = _EngineClient(k15, k1h, k4h)
    e._oi_hist, e.positions = {}, {}
    e.instruments = {SYM: {"kucoinSymbol": KSYM}}
    e.paper_trade = profile == hk.PROFILE_PRE_GEOMETRY
    e.pilot = SimpleNamespace(enabled=profile == hk.PROFILE_LIVE_PILOT)
    r, journal = runtime_with_bundle("SHADOW", bundle_profile=hk.PROFILE_LIVE_PILOT, engine_profile=profile)
    e.ai_runtime = r
    sig = signal()
    with patch.object(eng.TradingEngine, "_get_market_session", staticmethod(lambda: "ASIA")), \
            patch.object(eng.nexus_ai, "decide", decide), \
            patch.object(eng.time, "time", lambda: DECISION_TS / 1000 + 60):
        adjusted = e._session_score_adjustment(SYM, sig.score)                  # _scan_all_and_enter
        scan_ok = (adjusted >= int(cfg.MIN_ENTRY_SCORE)
                   and e._regime_allows_direction(sig.regime, sig.direction) and sig.expected_pnl > 0)
        sig.score = adjusted
        if not scan_ok:
            return {"eligible": False}
        nx = run(e._nexus_validate(sig))                                         # wrapped in production
        from bot.nexus_types import decision_validation_error
        if decision_validation_error(nx, sig.symbol, sig.direction, sig.entry, sig.sl, sig.tp) is not None \
                or nx.execution_allowed is not True:
            return {"eligible": False}
        out = run(e._ai_gate(sig, nx))                                           # THE HOOK
    rec = journal.db.rows[out.decision.decision_id]["record"]
    return {"eligible": True, "observation": rec["hook_observation"], "decision": out.decision,
            "features": rec["features"]}


def replay_hook(profile, decide, market_data):
    """Replay path to AI_RUNTIME_HOOK_POPULATION_V1 (same primitives as the replay)."""
    from bot.backtest import _closed_window_by_ts, _timestamp_index
    from bot.engine import TradingEngine
    from bot.professional_risk_adapter import conservative_cost_fraction
    k15, k1h, k4h = market_data
    sig = signal()
    w15 = _closed_window_by_ts(k15, _timestamp_index(k15), DECISION_TS, 15, 80)
    w1h = _closed_window_by_ts(k1h, _timestamp_index(k1h), DECISION_TS, 60, 50)
    w4h = _closed_window_by_ts(k4h, _timestamp_index(k4h), DECISION_TS, 240, 30)
    funnel = xp.funnel_flags(sig, DECISION_TS, TradingEngine)
    nx = decide(SYM, w15, w1h, w4h, entry=sig.entry, sl=sig.sl, tp=sig.tp)
    geometry = xp.production_geometry(sig, leverage=int(cfg.LEVERAGE), mmr=MMR,
                                      fee_multiplier=float(cfg.FEE_MULTIPLIER))
    nx2 = None
    if geometry["status"] == "ADJUSTED" and nx.execution_allowed:
        nx2 = decide(SYM, w15, w1h, w4h, entry=sig.entry, sl=geometry["sl"], tp=geometry["tp"])
    res = hk.evaluate_candidate(sig, decision_ts=DECISION_TS, funnel=funnel,
                                min_entry_score=float(cfg.MIN_ENTRY_SCORE), nx_initial=nx, geometry=geometry,
                                nx_final=nx2, cost_fraction=conservative_cost_fraction(SYM), profile=profile)
    if not res["eligible"]:
        return {"eligible": False, "stage": res["stage"], "geometry": geometry}
    fv, regime = hk.features(res["observation"], w15, w1h, w4h)
    return {"eligible": True, "observation": res["observation"].to_dict(), "fv": fv, "regime": regime,
            "geometry": geometry}


class GoldenHookParity(unittest.TestCase):
    def _compare(self, profile):
        m = market()
        e, r = engine_hook(profile, fake_decide(), m), replay_hook(profile, fake_decide(), m)
        self.assertTrue(e["eligible"] and r["eligible"])
        self.assertEqual(e["observation"], r["observation"])           # ts, entry, stop, RR, score, conf...
        d = e["decision"]
        self.assertEqual(d.timestamp, r["observation"]["decision_ts"])
        self.assertEqual(d.feature_hash, r["fv"].feature_hash())
        self.assertEqual(d.feature_schema_sha256, fx.schema_hash())
        self.assertEqual(d.regime, r["regime"])
        self.assertEqual(e["features"]["values"], r["fv"].values)
        self.assertEqual(e["features"]["missing"], list(r["fv"].missing))
        self.assertEqual(list(fx.MODEL_FEATURES), list(fx.MODEL_FEATURES))
        return e, r

    def test_live_pilot_hook_parity_with_compressed_stop(self):
        e, r = self._compare(hk.PROFILE_LIVE_PILOT)
        self.assertEqual(r["geometry"]["status"], "ADJUSTED")
        self.assertNotEqual(e["observation"]["stop"], 90.0)             # the hook sees the compressed stop
        self.assertGreater(e["observation"]["stop"], 90.0)
        self.assertEqual(e["observation"]["rr"], round(float(r["geometry"]["rr"]), 2))
        self.assertEqual(e["observation"]["strategy_score"], 70.0)      # 78 - 8 ASIA session penalty
        self.assertAlmostEqual(e["observation"]["nexus_confidence"],
                               50.0 + 100 * (100.0 - e["observation"]["stop"]) / 100.0)   # recheck decision

    def test_pre_geometry_profile_parity_uses_original_stop(self):
        e, _ = self._compare(hk.PROFILE_PRE_GEOMETRY)
        self.assertEqual(e["observation"]["stop"], 90.0)
        self.assertEqual(e["observation"]["rr"], 3.0)
        self.assertEqual(e["observation"]["profile"], hk.PROFILE_PRE_GEOMETRY)

    def test_nexus_rejected_candidate_never_reaches_the_hook(self):
        m = market()
        for profile in (hk.PROFILE_LIVE_PILOT, hk.PROFILE_PRE_GEOMETRY):
            dec_fn = fake_decide(reject_initial=True)
            self.assertFalse(engine_hook(profile, dec_fn, m)["eligible"])
            r = replay_hook(profile, dec_fn, m)
            self.assertEqual((r["eligible"], r["stage"]), (False, "NEXUS_REJECTED"))

    def test_recheck_rejected_after_compression_never_reaches_the_hook(self):
        m = market()
        dec_fn = fake_decide(reject_recheck=True)
        self.assertFalse(engine_hook(hk.PROFILE_LIVE_PILOT, dec_fn, m)["eligible"])
        r = replay_hook(hk.PROFILE_LIVE_PILOT, dec_fn, m)
        self.assertEqual(r["stage"], "NEXUS_RECHECK_REJECTED")
        self.assertTrue(replay_hook(hk.PROFILE_PRE_GEOMETRY, dec_fn, m)["eligible"])  # paper never rechecks

    def test_original_vs_compressed_stop_mismatch_is_detected(self):
        m = market()
        live = replay_hook(hk.PROFILE_LIVE_PILOT, fake_decide(), m)["observation"]
        pre = replay_hook(hk.PROFILE_PRE_GEOMETRY, fake_decide(), m)["observation"]
        self.assertNotEqual(live["stop"], pre["stop"])
        # A LIVE-pilot-trained bundle never becomes authoritative on the pre-geometry hook.
        r, _ = runtime_with_bundle("PAPER", bundle_profile=hk.PROFILE_LIVE_PILOT,
                                   engine_profile=hk.PROFILE_PRE_GEOMETRY)
        self.assertIn("AI_HOOK_PROFILE_MISMATCH", r.halts.active)
        # SHADOW records it explicitly instead of producing mislabelled evidence.
        e = engine_hook(hk.PROFILE_PRE_GEOMETRY, fake_decide(), m)
        self.assertFalse(e["decision"] is None)

    def test_shadow_profile_mismatch_is_labelled(self):
        r, j = runtime_with_bundle("SHADOW", bundle_profile=hk.PROFILE_LIVE_PILOT,
                                   engine_profile=hk.PROFILE_PRE_GEOMETRY)
        m = market()
        obs = hk.HookObservation(symbol=SYM, direction="LONG", decision_ts=DECISION_TS, entry=100.0, stop=80.0,
                                 rr=3.0, strategy_score=70.0, nexus_confidence=70.0, cost_fraction=0.002,
                                 profile=hk.PROFILE_PRE_GEOMETRY)
        out = run(r.gate(obs, *m, taker_fee=0.0006, now_ms=DECISION_TS + 60_000))
        self.assertEqual(out.reason, "SHADOW_PROFILE_MISMATCH")
        self.assertTrue(out.allow)


class HookPopulation(unittest.TestCase):
    def test_executable_but_nexus_rejected_row_is_excluded_from_training(self):
        rows = synthetic_rows(n=40)
        rows[0]["ai_hook_eligible"] = False            # executable, NEXUS rejected
        rows[1].pop("ai_hook_eligible")                 # legacy row without the flag
        data = tr.dataset(rows)
        self.assertNotIn(rows[0]["ts"], [r["ts"] for r in data])
        self.assertNotIn(rows[1]["ts"], [r["ts"] for r in data])
        self.assertEqual(len(data), 38)

    def test_hook_eligibility_stages(self):
        sig = signal()
        base = dict(decision_ts=DECISION_TS, min_entry_score=60, cost_fraction=0.002,
                    nx_initial=SimpleNamespace(execution_allowed=True, confidence=60.0),
                    geometry={"status": "SAFE"}, nx_final=None)
        ok_funnel = {"adjusted_score": 70, "regime_allows_direction": True, "expected_pnl_positive": True}
        self.assertTrue(hk.evaluate_candidate(sig, funnel=ok_funnel, **base)["eligible"])
        for k, v, stage in (("adjusted_score", 50, "SCAN_SESSION_ADJUSTED_SCORE"),
                            ("regime_allows_direction", False, "SCAN_REGIME_DIRECTION"),
                            ("expected_pnl_positive", False, "SCAN_EXPECTED_PNL")):
            self.assertEqual(hk.evaluate_candidate(sig, funnel={**ok_funnel, k: v}, **base)["stage"], stage)
        blocked = hk.evaluate_candidate(sig, funnel=ok_funnel, **{**base, "geometry": {"status": "BLOCK"}})
        self.assertEqual(blocked["stage"], "CROSS_GEOMETRY_BLOCK")
        self.assertTrue(hk.evaluate_candidate(sig, funnel=ok_funnel, profile=hk.PROFILE_PRE_GEOMETRY,
                                              **{**base, "geometry": {"status": "BLOCK"}})["eligible"])

    def test_bundle_binds_the_hook_population(self):
        _, ca, ra, man = constant_bundle()
        self.assertEqual((man["hook_population"], man["hook_profile"]), (hk.POPULATION, hk.TRAINING_PROFILE))
        t = dict(man, hook_population="EXECUTABLE_CANDIDATES")
        t["bundle_sha256"] = dec.bundle_sha(t)
        with self.assertRaisesRegex(Exception, "HOOK_POPULATION_MISMATCH"):
            dec.ModelBundle.load(json.dumps(t), json.dumps(ca), json.dumps(ra), pinned_bundle_sha=t["bundle_sha256"])

    def test_gate_requires_hook_population(self):
        art = passing_artifact()
        _ai(art)["population"] = "EXECUTABLE_CANDIDATES"
        self.assertIn("AI_POPULATION_NOT_RUNTIME_HOOK", ai_gate.evaluate(art)["blockers"])


class EffectiveExecutionParity(unittest.TestCase):
    def test_parity_is_incomplete_while_pre_score_is_absent(self):
        self.assertEqual(hk.EFFECTIVE_EXECUTION_PARITY, "INCOMPLETE")
        self.assertTrue(any("scoring.calculate" in m for m in hk.EFFECTIVE_EXECUTION_PARITY_MISSING))
        src = inspect.getsource(__import__("bot.nexus_oos_real_replay", fromlist=["x"]))
        self.assertNotIn("scoring.calculate(", src)

    def test_hook_edge_alone_cannot_promote_live(self):
        art = passing_artifact()
        g = ai_gate.evaluate(art)
        self.assertEqual((g["verdict"], g["ai_hook_edge"]), ("PASS", "PASS"))
        self.assertEqual(g["ai_effective_execution_parity"], "INCOMPLETE")
        self.assertEqual(g["ai_effective_execution_edge"], "BLOCK")
        self.assertEqual(g["live_historical_promotion"]["verdict"], "BLOCK")
        self.assertIn("AI_EFFECTIVE_EXECUTION_PARITY_INCOMPLETE", g["live_historical_promotion"]["blockers"])
        art["candidate_research"]["ai_effective_execution_parity"] = {"status": "COMPLETE"}
        self.assertEqual(ai_gate.evaluate(art)["live_historical_promotion"]["verdict"], "PASS")

    def test_lifecycle_live_requires_effective_edge(self):
        ev = {k: "PASS" for k in ("FORWARD_PAPER_EVIDENCE", "STAGE_C_CODE", "STAGE_C_AI_IDENTITY", "HUMAN_APPROVAL")}
        with self.assertRaises(lc.PromotionRefused):
            lc.promote("PAPER_CHALLENGER", "LIVE_CHAMPION", ev)
        self.assertEqual(lc.promote("PAPER_CHALLENGER", "LIVE_CHAMPION",
                                    {**ev, "AI_EFFECTIVE_EXECUTION_EDGE": "PASS"}), "LIVE_CHAMPION")


# ── Stage C: AI_LIVE ─────────────────────────────────────────────────────────
class AISources(tlre.FakeSources):
    def __init__(self, *, ai_at=None, approved_state="LIVE_CHAMPION"):
        super().__init__()
        b, _, _, _ = constant_bundle(lifecycle="LIVE_CHAMPION")
        self.ident = {"candidate_sha": tlre.SHA, "ai_version": dec.AI_VERSION, "bundle_sha256": b.sha256,
                      "policy_sha256": b.policy.sha256, "feature_schema_sha256": fx.schema_hash()}
        self.approved_state = approved_state
        line = aid.observation_line(candidate_sha=tlre.SHA, deployment_id="dep-A", mode="LIVE",
                                    ai_version=dec.AI_VERSION, bundle_sha256=self.ident["bundle_sha256"],
                                    policy_sha256=self.ident["policy_sha256"],
                                    feature_schema_sha256=self.ident["feature_schema_sha256"],
                                    now=ai_at or tlre.NOW - dt.timedelta(hours=1))
        self.logs["dep-A"].append("2026-09-24 11:00 WARNING " + line)

    def approved_ai_identity(self, candidate_sha):
        self._rec("approved_ai_identity")
        return {**self.ident, "lifecycle_state": self.approved_state}


def ai_envelope(src, **kw):
    ev = tlre.envelope(src, **kw)
    ev["ai_identity"] = dict(src.ident)
    ev["release_authority_kind"] = "AI_LIVE"
    return tlre.seal(ev, src)


class StageCAILive(unittest.TestCase):
    def test_ai_live_cannot_pass_when_ai_identity_blocks(self):
        src = AISources(approved_state="SHADOW_CHALLENGER")
        r = gate.evaluate_live(None, ai_envelope(src), sources=src, now=tlre.NOW, release_authority_kind="AI_LIVE")
        d = r.to_dict()
        self.assertEqual(d["stages"]["AI_IDENTITY"], "BLOCK")
        self.assertEqual(d["verdict"], "BLOCK")
        self.assertIn("AI_IDENTITY_NOT_PROVEN", d["blockers"])

    def test_ai_live_is_blocked_by_hook_only_evidence_even_with_identity(self):
        src = AISources()
        r = gate.evaluate_live(None, ai_envelope(src), sources=src, now=tlre.NOW, release_authority_kind="AI_LIVE")
        d = r.to_dict()
        self.assertEqual(d["stages"]["AI_IDENTITY"], "PASS")
        self.assertEqual(d["verdict"], "BLOCK")
        self.assertIn("AI_LIVE_PROMOTION_EVIDENCE_NOT_PROVEN", d["blockers"])
        for verdict, ai_stage in ((d["verdict"], d["stages"]["AI_IDENTITY"]),):
            self.assertFalse(verdict == "PASS" and ai_stage == "BLOCK")

    def test_nexus_only_semantics_unchanged_and_explicit(self):
        src = tlre.FakeSources()
        r = tlre.live(tlre.envelope(src), src)
        self.assertTrue(r.promote, r.blockers)
        self.assertEqual(r.to_dict()["stages"]["release_authority_kind"], "NEXUS_ONLY")
        self.assertEqual(rt.authorize_ai_live(r), ["STAGE_C_AI_IDENTITY_NOT_PASSED", "STAGE_C_NOT_AI_LIVE_RELEASE"])
        # an envelope carrying ai_identity evaluated as NEXUS_ONLY is ambiguous
        src2 = AISources()
        r2 = gate.evaluate_live(None, ai_envelope(src2), sources=src2, now=tlre.NOW)
        self.assertIn("RELEASE_AUTHORITY_KIND_AMBIGUOUS", r2.blockers)
        self.assertFalse(r2.promote)

    def test_human_approval_must_postdate_ai_identity_observation(self):
        from bot import live_release_evidence as lre
        late = AISources(ai_at=tlre.NOW - dt.timedelta(minutes=10))      # AI observation AFTER approval
        out = lre.verify(ai_envelope(late), sources=late, now=tlre.NOW, release_authority_kind="AI_LIVE")
        self.assertEqual(out["components"]["HUMAN_APPROVAL_EVIDENCE"], "BLOCK")
        ok = AISources()
        out2 = lre.verify(ai_envelope(ok), sources=ok, now=tlre.NOW, release_authority_kind="AI_LIVE")
        self.assertEqual(out2["components"]["HUMAN_APPROVAL_EVIDENCE"], "PASS")
        self.assertEqual(out2["ai_identity"]["verdict"], "PASS")

    def test_approval_digest_binds_ai_identity(self):
        from bot import live_release_evidence as lre
        src = AISources()
        ev = ai_envelope(src)
        ev["ai_identity"]["policy_sha256"] = "9" * 64                     # edited after approval
        out = lre.verify(ev, sources=src, now=tlre.NOW, release_authority_kind="AI_LIVE")
        self.assertEqual(out["components"]["HUMAN_APPROVAL_EVIDENCE"], "BLOCK")


# ── durable journal integrity and transitions ────────────────────────────────
def gate_obs(r, **over):
    kw = dict(symbol="BTCUSDT", direction="LONG", decision_ts=DECISION_TS, entry=100.0, stop=99.0, rr=2.5,
              strategy_score=72.0, nexus_confidence=65.0, cost_fraction=0.0022, profile=r.hook_profile)
    kw.update(over)
    k15 = candles(120, M15, DECISION_TS)
    return run(r.gate(hk.HookObservation(**kw), k15, candles(60, H1, DECISION_TS, seed=2),
                      candles(40, H4, DECISION_TS, seed=3), taker_fee=0.0006, now_ms=DECISION_TS + 60_000))


def approved(db=None):
    db = db or FakeDB()
    r, _ = runtime_with_bundle("PAPER", db=db)
    run(r.recover(lambda oid: asyncio.sleep(0, "FOUND")))
    out = gate_obs(r)
    assert out.allow, out.reason
    return r, db, out.decision


class JournalIntegrity(unittest.TestCase):
    def test_hash_changes_after_every_status_mutation_and_stays_valid(self):
        r, db, d = approved()
        seen = []
        for st, extra in (("APPROVED", {}), ("INTENT_CREATED", {"client_oid": "bgx7-1"}),
                          ("SUBMITTED", {"order_id": "o1"}), ("FILLED", {}), ("CLOSED", {})):
            if st != "APPROVED":
                run(r.journal.set_status(d.decision_id, st, **extra))
            rec = db.rows[d.decision_id]["record"]
            self.assertEqual(rec["record_sha256"], rt.record_digest(rec))
            rt.verify_record(d.decision_id, st, rec)
            seen.append(rec["record_sha256"])
        self.assertEqual(len(set(seen)), len(seen))

    def test_tampered_record_fails_verification(self):
        r, db, d = approved()
        db.rows[d.decision_id]["record"]["decision"]["side"] = "SHORT"
        with self.assertRaises(rt.JournalIntegrityError):
            run(r.journal.get(d.decision_id))
        db2 = FakeDB()
        r2, _, d2 = approved(db2)
        db2.rows[d2.decision_id]["status"] = "CLOSED"                       # status column forged
        with self.assertRaises(rt.JournalIntegrityError):
            run(r2.journal.get(d2.decision_id))

    def test_restart_from_tampered_record_halts(self):
        r, db, d = approved()
        run(r.bind_intent(d, "bgx7-tamper"))
        db.rows[d.decision_id]["record"]["client_oid"] = "bgx7-other"
        r2, _ = runtime_with_bundle("PAPER", db=db)
        looked = []

        async def lookup(oid):
            looked.append(oid)
            return "FOUND"
        run(r2.recover(lookup))
        self.assertIn("AI_JOURNAL_INTEGRITY_FAILURE", r2.halts.active)
        self.assertEqual(looked, [])
        self.assertFalse(r2.recovered)
        self.assertFalse(gate_obs(r2, decision_ts=DECISION_TS).allow)

    def test_invalid_transition_fails_closed(self):
        r, db, d = approved()
        run(r.bind_intent(d, "bgx7-t"))
        run(r.journal.set_status(d.decision_id, "SUBMITTED"))
        with self.assertRaises(rt.InvalidJournalTransition):
            run(r.journal.set_status(d.decision_id, "APPROVED"))
        run(r.mark(d, "INTENT_CREATED"))                                    # backwards via engine hook
        self.assertIn("AI_JOURNAL_INTEGRITY_FAILURE", r.halts.active)
        for old, new in (("ABSTAIN", "INTENT_CREATED"), ("RECONCILED_NOT_FOUND", "SUBMITTED"),
                         ("SHADOW_TRADE", "INTENT_CREATED"), ("CLOSED", "FILLED")):
            with self.assertRaises(rt.InvalidJournalTransition):
                rt.check_transition(old, new)

    def test_shadow_duplicate_event_is_not_rewritten(self):
        r, j = runtime_with_bundle("SHADOW")
        a, b = gate_obs(r), gate_obs(r)
        self.assertEqual(a.decision.decision_id, b.decision.decision_id)
        self.assertEqual(len(j.db.rows), 1)


# ── model selection stability ────────────────────────────────────────────────
def _step(**policy_over):
    pol = dec.DecisionPolicy(0.55, 0.05, allowed_directions=("LONG",), supported_regimes=("TREND_UP", "RANGE"))
    pj = {**pol.to_json(), **policy_over}
    return {"selected_classifier": ["LOGISTIC_L2", {"l2": 1.0}], "selected_regressor": ["RIDGE", {"l2": 1.0}],
            "policy": pj}


class SelectionStability(unittest.TestCase):
    def test_supported_regimes_difference_is_unstable(self):
        self.assertEqual(tr.selection_stability([_step(), _step(supported_regimes=["TREND_UP"])])["status"],
                         "MODEL_SELECTION_UNSTABLE")

    def test_probability_authorization_difference_is_unstable(self):
        self.assertEqual(tr.selection_stability([_step(), _step(probability_authorizes=False)])["status"],
                         "MODEL_SELECTION_UNSTABLE")
        self.assertEqual(tr.selection_stability([_step(), _step(uncertainty_buffer_r=0.1)])["status"],
                         "MODEL_SELECTION_UNSTABLE")

    def test_identical_policy_sha_is_stable(self):
        s = tr.selection_stability([_step(), copy.deepcopy(_step())])
        self.assertEqual(s["status"], "MODEL_SELECTION_STABLE")
        self.assertEqual(len(set(s["policy_sha256_by_step"])), 1)


# ── isolated SHADOW observer ─────────────────────────────────────────────────
class ShadowObserverIsolation(unittest.TestCase):
    ENV = {"AI_EXECUTION_MODE": "SHADOW"}

    def test_refuses_credentials_and_non_shadow_mode(self):
        for k in so.FORBIDDEN_CREDENTIALS:
            with self.assertRaises(so.ObserverRefused):
                so.preflight({**self.ENV, k: "x"})
        with self.assertRaises(so.ObserverRefused):
            so.preflight({"AI_EXECUTION_MODE": "PAPER"})
        so.preflight(self.ENV)

    def test_client_has_no_mutation_capability(self):
        from bot.nexus_oos_real_replay import PublicKuCoinFuturesClient
        c = PublicKuCoinFuturesClient()
        so.assert_read_only(c)
        with self.assertRaises(RuntimeError):
            run(c._get("/api/v1/orders", {}, auth=True))
        from bot.kucoin import KuCoinClient
        with self.assertRaises(so.ObserverRefused):
            so.assert_read_only(KuCoinClient.__new__(KuCoinClient))
        with self.assertRaises(so.ObserverRefused):
            so.ShadowObserver(self.ENV, client=KuCoinClient.__new__(KuCoinClient), manifest={})

    def test_observer_never_builds_an_engine_or_lease(self):
        src = inspect.getsource(so)
        for bad in ("TradingEngine(", "execution_ownership", "place_order(", "wait_for_live_execution_ownership",
                    "KuCoinClient(", ".post(", "set_position_stops("):
            self.assertNotIn(bad, src)

    def test_observer_hook_path_journals_shadow_decisions_with_zero_orders(self):
        from bot import nexus_oos_replay_manifest as rm
        r, j = runtime_with_bundle("SHADOW")
        obs = so.ShadowObserver(self.ENV, client=SimpleNamespace(), runtime=r, manifest=rm.load(), symbols=[SYM])
        obs.mmr_proxy[SYM] = 0.004
        m = market()
        with patch("bot.strategy.Analyzer.analyze_mtf", lambda self, *a, **k: signal(sl=99.5, tp=101.5)), \
                patch("bot.nexus_ai.decide", fake_decide()):
            out = run(obs.evaluate(SYM, *m, decision_ts=DECISION_TS, now_ms=DECISION_TS + 60_000))
        self.assertEqual(out["stage"], "AI_HOOK", out)
        self.assertTrue(out["allow"])
        self.assertEqual(obs.orders_sent, 0)
        rec = next(iter(j.db.rows.values()))
        self.assertIn(rec["status"], ("SHADOW_TRADE", "SHADOW_ABSTAIN"))
        self.assertEqual(rec["record"]["hook_observation"]["profile"], hk.PROFILE_LIVE_PILOT)

    def test_observer_lifecycle_makes_no_claims(self):
        self.assertEqual(lc.OBSERVER_CLAIMS, {"edge_claim": False, "order_authority": False,
                                              "live_authority": False})
        self.assertEqual(lc.ALLOWED_MODES["SHADOW_OBSERVER"], ("SHADOW",))
        self.assertFalse(any(k[0] == "SHADOW_OBSERVER" for k in lc.REQUIREMENTS))


if __name__ == "__main__":
    unittest.main()
