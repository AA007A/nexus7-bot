"""Phase 7: AI decision authority, authority chain, execution modes, journal.

The AI decides opportunity quality only. Deterministic validation, risk,
loss budget, liquidation, halts and the Stage-C LIVE gate always win.
"""
import copy
import inspect
import json
import math
import random
import unittest
from decimal import Decimal

import numpy as np

from bot import risk_policy as rp
from bot.ai import authority_chain as ac
from bot.ai import calibration as cal
from bot.ai import decision as dec
from bot.ai import execution_mode as em
from bot.ai import features as fx
from bot.ai import halt as hl
from bot.ai import journal as jr
from bot.ai import models as mdl
from bot.ai import readiness as rd
from bot.ai import shadow as sh
from bot.ai import state_machine as smx
from bot.ai import training as tr

T0 = 1_760_054_400_000
M15, H1, H4 = 15 * 60_000, 60 * 60_000, 4 * 60 * 60_000


def candles(n, tf, end_ts, start=100.0, drift=0.0005, seed=1):
    rng = random.Random(seed)
    out, px = [], start
    for i in range(n):
        o = px
        c = o * (1 + drift + rng.gauss(0, 0.002))
        h, lo = max(o, c) * 1.001, min(o, c) * 0.999
        out.append({"ts": end_ts - (n - i) * tf, "o": o, "h": h, "l": lo, "c": c, "v": 100 + rng.random() * 50})
        px = c
    return out


DECISION_TS = T0 + 100 * M15


def feature_vector(direction="LONG", **over):
    k15 = candles(80, M15, DECISION_TS)
    k1h = candles(60, H1, DECISION_TS, seed=2)
    k4h = candles(40, H4, DECISION_TS, seed=3)
    kw = dict(decision_ts=DECISION_TS, direction=direction, strategy_score=72, entry=100.0, stop=99.0,
              rr=2.5, cost_fraction=0.0022, nexus_confidence=65.0)
    kw.update(over)
    return fx.compute_features(k15, k1h, k4h, **kw)


POLICY = dec.DecisionPolicy(min_p_profitable=0.55, min_expected_net_r=0.05, allowed_directions=("LONG",),
                            supported_regimes=dec.TRADABLE_REGIMES)


def constant_bundle(p=0.62, gross_r=0.5, policy=POLICY, lifecycle="SHADOW_CHALLENGER", hook_profile=None,
                    training_code_sha="t" * 40):
    d = len(fx.MODEL_FEATURES)
    clf = mdl.LogisticL2.from_params({"l2": 1.0, "iters": 1, "std": {"mean": [0.0] * d, "scale": [1.0] * d},
                                      "w": [0.0] * d, "b": math.log(p / (1 - p))})
    reg = mdl.Ridge.from_params({"l2": 1.0, "std": {"mean": [0.0] * d, "scale": [1.0] * d},
                                 "w": [0.0] * d, "b": gross_r})
    common = dict(feature_names=fx.MODEL_FEATURES, feature_schema_sha256=fx.schema_hash(),
                  hyperparameters={}, training_manifest={"rows": 0}, training_code_sha=None,
                  created_at="2026-09-24T00:00:00Z", training_period={})
    ca = mdl.artifact(clf, role="A", calibration={"kind": "IDENTITY"}, **common)
    ra = mdl.artifact(reg, role="B", **common)
    man = dec.bundle_manifest(classifier_artifact=ca, regressor_artifact=ra, calibration={"kind": "IDENTITY"},
                              policy=policy, training_code_sha=training_code_sha, dataset_manifest_sha256="d" * 64,
                              training_period={}, selection_evidence={}, created_at="2026-09-24T00:00:00Z",
                              lifecycle_state=lifecycle, hook_profile=hook_profile)
    b = dec.ModelBundle.load(json.dumps(man), json.dumps(ca), json.dumps(ra),
                             pinned_bundle_sha=man["bundle_sha256"])
    return b, ca, ra, man


def authority(p=0.62, gross_r=0.5, policy=POLICY, pinned=True, clock=None):
    b, _, _, _ = constant_bundle(p, gross_r, policy)
    kw = {"clock": clock} if clock else {}
    return dec.AIDecisionAuthority(b, pinned_bundle_sha=b.sha256 if pinned else "0" * 64,
                                   candidate_sha="c" * 40, **kw)


def decide(auth, direction="LONG", regime="TREND_UP", fees_r=0.1, slippage_r=0.05, **kw):
    return auth.decide(symbol="BTCUSDT", direction=direction, features=feature_vector(direction),
                       regime=regime, fees_r=fees_r, slippage_r=slippage_r, **kw)


def policy(**over):
    base = dict(leverage=50.0, max_risk_pct=0.01, max_margin_pct=0.50, max_drawdown=0.50, max_positions=2,
                daily_stop_loss_pct=0.03, daily_stop_loss_abs=0.0, min_rr_ratio=2.0)
    base.update(over)
    return rp.RiskPolicy(**base)


RULES = rp.QuantityRules(Decimal("0.001"), Decimal("1"), Decimal("1"), Decimal("0"))
GEOM = ac.Geometry(entry=100.0, stop=99.0, tp1=101.5, tp2=102.5)


def ctx(**over):
    base = dict(policy=policy(), equity=1000.0, available=1000.0, drawdown=0.05, daily_realized_loss=0.0,
                day_start_balance=1000.0, rules=RULES, cost_fraction=0.0022, maintenance_margin_rate=0.005)
    base.update(over)
    return ac.RiskContext(**base)


class Decisions(unittest.TestCase):
    def test_abstain_is_first_class(self):
        d = decide(authority(p=0.40))
        self.assertEqual(d.side, dec.ABSTAIN)
        self.assertIn("PROBABILITY_BELOW_MIN", d.vetoes)
        self.assertFalse(d.is_trade)

    def test_long_decision(self):
        d = decide(authority())
        self.assertEqual(d.side, dec.LONG, d.vetoes)
        self.assertTrue(d.is_trade)
        for f in ("decision_id", "timestamp", "symbol", "probability_raw", "probability_calibrated",
                  "expected_r", "expected_net_r_after_costs", "regime", "model_version", "model_sha256",
                  "feature_version", "feature_hash", "reason_codes", "vetoes", "data_freshness_ms",
                  "decision_latency_ms", "candidate_sha"):
            self.assertIn(f, d.to_dict())

    def test_short_requires_its_own_enablement(self):
        d = decide(authority(), direction="SHORT")
        self.assertEqual(d.side, dec.ABSTAIN)
        self.assertIn("DIRECTION_NOT_ENABLED_SHORT", d.vetoes)
        pol = dec.DecisionPolicy(0.55, 0.05, allowed_directions=("LONG", "SHORT"),
                                 supported_regimes=dec.TRADABLE_REGIMES)
        self.assertEqual(decide(authority(policy=pol), direction="SHORT").side, dec.SHORT)

    def test_stale_data_abstains(self):
        d = decide(authority(), now_ms=DECISION_TS + 3 * M15)
        self.assertIn("STALE_DATA", d.vetoes)

    def test_unsupported_regime_abstains(self):
        for reg in ("UNKNOWN", "EXTREME"):
            self.assertIn("UNSUPPORTED_REGIME", decide(authority(), regime=reg).vetoes)

    def test_model_hash_mismatch(self):
        self.assertIn("MODEL_HASH_MISMATCH", decide(authority(pinned=False)).vetoes)
        _, ca, ra, man = constant_bundle()
        with self.assertRaises(mdl.ModelIntegrityError):
            dec.ModelBundle.load(json.dumps(man), json.dumps(ca), json.dumps(ra), pinned_bundle_sha="0" * 64)
        tampered = copy.deepcopy(ca)
        tampered["params"]["b"] = 5.0
        with self.assertRaises(mdl.ModelIntegrityError):
            mdl.load_artifact(json.dumps(tampered), expected_sha256=ca["sha256"],
                              expected_feature_schema=fx.schema_hash())

    def test_feature_schema_mismatch(self):
        fv = feature_vector()
        fv.schema_sha256 = "f" * 64
        d = authority().decide(symbol="BTCUSDT", direction="LONG", features=fv, regime="TREND_UP",
                               fees_r=0.1, slippage_r=0.05)
        self.assertIn("FEATURE_SCHEMA_MISMATCH", d.vetoes)
        _, ca, _, _ = constant_bundle()
        with self.assertRaises(mdl.ModelIntegrityError):
            mdl.load_artifact(json.dumps(ca), expected_sha256=ca["sha256"], expected_feature_schema="x")

    def test_negative_expected_net_r_is_vetoed(self):
        d = decide(authority(gross_r=0.10), fees_r=0.10, slippage_r=0.05)
        self.assertIn("NET_EDGE_BELOW_MIN", d.vetoes)
        self.assertLess(d.expected_net_r_after_costs, 0)

    def test_fees_turn_positive_gross_ev_into_rejection(self):
        ok = decide(authority(gross_r=0.30), fees_r=0.0, slippage_r=0.0)
        self.assertTrue(ok.is_trade, ok.vetoes)
        self.assertGreater(ok.expected_r, 0)
        bad = decide(authority(gross_r=0.30), fees_r=0.15, slippage_r=0.10)
        self.assertGreater(bad.expected_r, 0)                       # gross EV still positive
        self.assertIn("NET_EDGE_BELOW_MIN", bad.vetoes)

    def test_latency_budget(self):
        ticks = iter([0.0, 5.0])
        d = decide(authority(clock=lambda: next(ticks)))
        self.assertIn("DECISION_LATENCY_EXCEEDED", d.vetoes)

    def test_missing_required_feature_abstains_and_causality_enforced(self):
        k15 = candles(80, M15, DECISION_TS)
        k15.append({"ts": DECISION_TS, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1})   # still open
        with self.assertRaises(fx.FeatureCausalityError):
            fx.compute_features(k15, [], [], decision_ts=DECISION_TS, direction="LONG", strategy_score=70,
                                entry=100, stop=99, rr=2, cost_fraction=0.002)
        short = fx.compute_features(candles(10, M15, DECISION_TS), [], [], decision_ts=DECISION_TS,
                                    direction="LONG", strategy_score=70, entry=100, stop=99, rr=2,
                                    cost_fraction=0.002)
        self.assertTrue(short.abstain_required)
        d = authority().decide(symbol="X", direction="LONG", features=short, regime="TREND_UP",
                               fees_r=0.1, slippage_r=0.05)
        self.assertIn("REQUIRED_FEATURE_MISSING", d.vetoes)

    def test_live_only_features_are_not_model_inputs(self):
        for name in ("funding_rate", "open_interest_delta", "orderbook_imbalance"):
            self.assertNotIn(name, fx.MODEL_FEATURES)


class Calibration(unittest.TestCase):
    def test_platt_fitted_on_validation_improves_miscalibrated_scores(self):
        rng = np.random.default_rng(0)
        z = rng.normal(size=4000)
        y = (rng.random(4000) < 1 / (1 + np.exp(-z))).astype(float)
        raw = 1 / (1 + np.exp(-3 * z))                   # overconfident
        c = cal.Platt().fit(raw[:2000], y[:2000])
        self.assertLess(cal.brier(c.transform(raw[2000:]), y[2000:]), cal.brier(raw[2000:], y[2000:]))
        rep = cal.report(c.transform(raw[2000:]), y[2000:])
        for k in ("brier", "brier_base_rate", "log_loss", "ece", "reliability", "beats_base_rate"):
            self.assertIn(k, rep)
        self.assertTrue(rep["beats_base_rate"])
        noise = cal.report(np.full(2000, 0.5), y[2000:])
        self.assertFalse(noise["beats_base_rate"])       # uninformative => never "predictive"


class RiskBeatsAI(unittest.TestCase):
    def test_approved_ai_passes_full_chain(self):
        r = ac.run(decide(authority()), GEOM, ctx(), halted=False)
        self.assertTrue(r.approved, r.vetoes)
        self.assertTrue(r.intent.client_oid.startswith("bgxai"))

    def test_risk_sizing_veto_beats_ai(self):
        r = ac.run(decide(authority(p=0.99)), GEOM, ctx(equity=0.01, available=0.01), halted=False)
        self.assertFalse(r.approved)
        self.assertEqual(r.stage, "RISK_SIZING")

    def test_drawdown_veto_beats_ai(self):
        r = ac.run(decide(authority(p=0.99)), GEOM, ctx(drawdown=0.6), halted=False)
        self.assertEqual((r.approved, r.stage), (False, "DRAWDOWN"))

    def test_daily_stop_veto_beats_ai(self):
        r = ac.run(decide(authority(p=0.99)), GEOM, ctx(daily_realized_loss=31.0), halted=False)
        self.assertEqual((r.approved, r.stage), (False, "DAILY_STOP"))

    def test_liquidation_veto_beats_ai(self):
        hot = dict(maintenance_margin_rate=0.9, policy=policy(leverage=125.0))
        normal = ac.run(decide(authority(p=0.99)), GEOM, ctx(policy=policy(leverage=125.0)), halted=False)
        capped = ac.run(decide(authority(p=0.99)), GEOM, ctx(**hot), halted=False)
        self.assertIn("binding=LIQUIDATION", capped.intent.sizing)
        self.assertLess(capped.intent.qty, normal.intent.qty)          # confidence cannot lift it
        big_min = rp.QuantityRules(Decimal("0.001"), Decimal("1"), Decimal("6000"), Decimal("0"))
        r = ac.run(decide(authority(p=0.99)), GEOM, ctx(rules=big_min, **hot), halted=False)
        self.assertFalse(r.approved)
        self.assertEqual(r.stage, "RISK_SIZING")

    def test_halt_and_kill_switch_beat_ai(self):
        self.assertEqual(ac.run(decide(authority()), GEOM, ctx(), halted=True,
                                halt_reasons=["STALE_MARKET_DATA"]).stage, "HALT")
        self.assertEqual(ac.run(decide(authority()), GEOM, ctx(kill_switch=True), halted=False).stage, "HALT")

    def test_invalid_geometry_is_blocked(self):
        bad = ac.Geometry(entry=100.0, stop=101.0, tp1=102.0, tp2=103.0)
        self.assertEqual(ac.run(decide(authority()), bad, ctx(), halted=False).stage, "STRATEGY_VALIDATION")

    def test_confidence_cannot_upsize(self):
        lo = ac.run(decide(authority(p=0.60)), GEOM, ctx(), halted=False)
        hi = ac.run(decide(authority(p=0.99)), GEOM, ctx(), halted=False)
        self.assertEqual(lo.intent.qty, hi.intent.qty)
        src = inspect.getsource(ac.run)
        for word in ("confidence", "probability", "ai_score", "expected_r"):
            self.assertNotIn(word, src)


class Idempotency(unittest.TestCase):
    def test_duplicate_decision_does_not_duplicate_order(self):
        a, b = decide(authority()), decide(authority())
        self.assertEqual(a.decision_id, b.decision_id)
        ia = ac.run(a, GEOM, ctx(), halted=False).intent
        ib = ac.run(b, GEOM, ctx(), halted=False).intent
        self.assertEqual(ia.client_oid, ib.client_oid)
        m = smx.AutonomousStateMachine(state="RISK_CHECK")
        sent = []
        m.submit(ia, lambda i: sent.append(i.client_oid) or "ACK", lambda oid: "FOUND")
        m.state = "RISK_CHECK"
        m.submit(ib, lambda i: sent.append(i.client_oid) or "ACK", lambda oid: "FOUND")
        self.assertEqual(sent, [ia.client_oid])

    def test_timeout_reconciles_before_retry(self):
        intent = ac.run(decide(authority()), GEOM, ctx(), halted=False).intent
        m = smx.AutonomousStateMachine(state="RISK_CHECK")
        sent, looked = [], []

        def send(i):
            sent.append(1)
            raise TimeoutError

        res = m.submit(intent, send, lambda oid: looked.append(oid) or "FOUND")
        self.assertEqual(res, "FOUND")
        self.assertEqual(len(sent), 1)
        self.assertEqual(looked, [intent.client_oid])
        self.assertEqual(m.state, "CONFIRM_ORDER")

    def test_restart_during_pending_order_reconciles_not_resends(self):
        intent = ac.run(decide(authority()), GEOM, ctx(), halted=False).intent
        m = smx.AutonomousStateMachine(state="RISK_CHECK", intents={intent.client_oid: "PENDING_UNKNOWN"})

        def boom(oid):
            raise ConnectionError

        sent = []
        self.assertEqual(m.submit(intent, lambda i: sent.append(1) or "ACK", boom), "PENDING_UNKNOWN")
        self.assertEqual(sent, [])
        self.assertEqual(m.state, "HALT")

    def test_tpsl_install_failure_invokes_fail_safe_and_halts(self):
        m = smx.AutonomousStateMachine(state="CONFIRM_ORDER")
        called = []

        def install():
            raise RuntimeError("tpsl rejected")

        self.assertFalse(m.protect(install, lambda: True, lambda: called.append("FAIL_SAFE")))
        self.assertEqual(called, ["FAIL_SAFE"])
        self.assertEqual(m.state, "HALT")

    def test_restart_with_open_position_resumes_management(self):
        m = smx.AutonomousStateMachine()
        m.go("SYNC_EXCHANGE", reason="restart")
        m.go("MANAGE_POSITION", reason="exchange reports open position")
        self.assertEqual(m.state, "MANAGE_POSITION")
        self.assertEqual([t["to"] for t in m.transitions], ["SYNC_EXCHANGE", "MANAGE_POSITION"])

    def test_illegal_transition_halts(self):
        m = smx.AutonomousStateMachine()
        with self.assertRaises(smx.IllegalTransition):
            m.go("PLACE_ORDER")
        self.assertEqual(m.state, "HALT")


class ExecutionModes(unittest.TestCase):
    def _obs(self, runner, **over):
        kw = dict(symbol="BTCUSDT", direction="LONG", features=feature_vector(), regime="TREND_UP",
                  geometry=GEOM, risk_ctx=ctx(), halt=hl.HaltController(), fees_r=0.1, slippage_r=0.05)
        kw.update(over)
        return runner.observe(**kw)

    def test_shadow_sends_zero_orders_and_runs_champion_challenger(self):
        r = sh.ShadowRunner(champion=authority(), challenger=authority(p=0.40))
        out = self._obs(r)
        self.assertTrue(out["CHAMPION"].approved)
        self.assertFalse(out["CHALLENGER"].approved)
        rep = r.report()
        self.assertEqual(rep["orders_sent"], 0)
        self.assertEqual(rep["would_send"], 1)
        self.assertEqual(rep["journal_records"], 2)
        self.assertTrue(rep["journal_intact"])
        for k in ("p50", "p95", "p99"):
            self.assertIsNotNone(rep["latency_ms"][k])

    def test_paper_sends_zero_real_orders(self):
        intent = ac.run(decide(authority()), GEOM, ctx(), halted=False).intent
        pa = em.PaperAdapter(taker_fee=0.0006, slippage_rate=0.0005, tick_size=0.1, lot_base=0.001,
                             min_base=0.001)
        f1 = pa.submit(intent, best_bid=99.9, best_ask=100.0)
        f2 = pa.submit(intent, best_bid=99.9, best_ask=100.0)
        self.assertEqual(f1["status"], "FILLED")
        self.assertTrue(f2["duplicate"])
        self.assertGreaterEqual(f1["price"], 100.0)
        self.assertEqual(pa.orders_sent, 0)
        tiny = copy.copy(intent)
        tiny.client_oid, tiny.qty = "bgxai-tiny", 0.0001
        self.assertEqual(pa.submit(tiny, best_bid=99.9, best_ask=100.0)["status"], "REJECTED_MIN_LOT")

    def test_live_cannot_initialize_without_stage_c(self):
        from bot import nexus_oos_promotion_gate as gate
        with self.assertRaises(em.LiveNotAuthorized):
            em.resolve_mode({"EXECUTION_MODE": "LIVE"})
        with self.assertRaises(em.LiveNotAuthorized):
            em.resolve_mode({"EXECUTION_MODE": "LIVE"}, stage_c_result=gate.evaluate_live({}, None))
        with self.assertRaises(em.LiveNotAuthorized):
            em.LiveAdapterGuard(sender=object(), stage_c_result=None)
        self.assertEqual(em.resolve_mode({"EXECUTION_MODE": "paper"}), "PAPER")
        self.assertEqual(em.resolve_mode({"EXECUTION_MODE": "nonsense"}), "SHADOW")
        self.assertEqual(em.resolve_mode({}), "SHADOW")

    def test_unknown_model_cannot_trade(self):
        auth = dec.AIDecisionAuthority(None, pinned_bundle_sha=None)
        d = decide(auth)
        self.assertIn("MODEL_UNAVAILABLE", d.vetoes)
        self.assertIn("POLICY_ABSTAIN_ALL", d.vetoes)
        self.assertFalse(ac.run(d, GEOM, ctx(), halted=False).approved)
        self.assertFalse(rd.build(None, candidate_sha="c" * 40)["shadow_challenger"]["created"])

    def test_ai_exits_are_shadow_only(self):
        rec = dec.exit_recommendation(symbol="BTCUSDT", unrealized_r=-0.4, model_hint=-0.9)
        self.assertEqual((rec["action"], rec["mode"], rec["executable"]), ("EXIT", "SHADOW_ONLY", False))


class JournalAndHalts(unittest.TestCase):
    def test_journal_reproduces_decisions_and_detects_tampering(self):
        auth = authority()
        fv = feature_vector()
        d1 = auth.decide(symbol="BTCUSDT", direction="LONG", features=fv, regime="TREND_UP",
                         fees_r=0.1, slippage_r=0.05)
        j = jr.DecisionJournal()
        j.append(decision=d1.to_dict(), feature_snapshot={"values": fv.values}, chain={"approved": True})
        snap = j.records[0]["feature_snapshot"]["values"]
        fv2 = fx.FeatureVector(dict(snap), fv.missing, fv.decision_ts, fv.newest_candle_close_ts)
        d2 = auth.decide(symbol="BTCUSDT", direction="LONG", features=fv2, regime="TREND_UP",
                         fees_r=0.1, slippage_r=0.05)
        self.assertEqual(dec.decision_digest(d1), dec.decision_digest(d2))
        self.assertTrue(j.verify())
        j.records[0]["decision"]["side"] = "SHORT"
        self.assertFalse(j.verify())

    def test_ai_cannot_clear_a_halt(self):
        h = hl.HaltController()
        h.raise_halt("UNPROTECTED_POSITION", source="RECONCILER")
        with self.assertRaises(hl.HaltClearRefused):
            h.clear("UNPROTECTED_POSITION", actor="AI", reason="confident")
        self.assertTrue(h.halted)
        h.clear("UNPROTECTED_POSITION", actor=hl.OPERATOR, reason="position verified protected")
        self.assertFalse(h.halted)


class ModelSafety(unittest.TestCase):
    def test_pickle_and_non_json_are_refused(self):
        import pickle
        payload = pickle.dumps({"a": 1})
        with self.assertRaises(mdl.ModelIntegrityError):
            mdl.load_artifact(payload, expected_sha256="x", expected_feature_schema=fx.schema_hash())
        with self.assertRaises(mdl.ModelIntegrityError):
            mdl.load_artifact("not json", expected_sha256="x", expected_feature_schema=fx.schema_hash())
        src = inspect.getsource(mdl)
        self.assertNotIn("pickle.load", src)
        self.assertNotIn("eval(", src)

    def test_models_fit_and_round_trip(self):
        rng = np.random.default_rng(1)
        X = rng.normal(size=(600, 4))
        y = (X[:, 0] + 0.3 * rng.normal(size=600) > 0).astype(float)
        for m in (mdl.LogisticL2(1.0), mdl.BoostedStumps(n_estimators=20, min_leaf=20)):
            m.fit(X, y)
            p = m.predict_proba(X)
            self.assertLess(cal.brier(p, y), 0.2)
            again = type(m).from_params(json.loads(json.dumps(m.params())))
            np.testing.assert_allclose(again.predict_proba(X), p)


class TrainingDiscipline(unittest.TestCase):
    def _rows(self, n=1600, seed=5):
        rng = random.Random(seed)
        rows = []
        d = len(fx.MODEL_FEATURES)
        for i in range(n):
            f = [rng.gauss(0, 1) for _ in range(d)]
            f[fx.MODEL_FEATURES.index("cost_fraction")] = 0.002
            f[fx.MODEL_FEATURES.index("stop_distance_pct")] = 0.01
            f[fx.MODEL_FEATURES.index("nexus_confidence")] = 60.0
            ts = T0 + i * 3 * H1
            r = 0.8 * f[0] + rng.gauss(0, 1.0)
            rows.append({"ts": ts, "outcome_end_ts": ts + H1, "executable": True, "approved": i % 3 == 0,
                         "ai_hook_eligible": True,
                         "outcome_status": "RESOLVED", "r": r, "gross_r": r + 0.2, "ai_features": f,
                         "direction": "LONG" if i % 2 else "SHORT", "symbol": "BTCUSDT",
                         "ai_regime": "TREND_UP"})
        return rows

    def test_walk_forward_windows_and_no_test_leakage(self):
        rows = self._rows()
        a = tr.walk_forward(rows, required_ms=inf_day(), samples=200)
        self.assertEqual(a["status"], "OK")
        self.assertEqual(a["data_label"], "HISTORICAL_OOS_PREVIOUSLY_INSPECTED")
        self.assertEqual([(s["train_folds"], s["validation_fold"], s["test_fold"]) for s in a["steps"]],
                         [([1], 2, 3), ([1, 2], 3, 4)])
        # Changing TEST outcomes must not change anything selected (model, policy).
        b_rows = copy.deepcopy(rows)
        for r in b_rows:
            if int(r["ts"]) >= a["folds"][3]["decision_start_ts"]:
                r["r"] = -abs(r["r"]) - 5
        b = tr.walk_forward(b_rows, required_ms=inf_day(), samples=200)
        self.assertEqual(a["steps"][1]["policy"], b["steps"][1]["policy"])
        self.assertEqual(a["steps"][1]["selected_classifier"], b["steps"][1]["selected_classifier"])

    def test_readiness_is_sanitized_and_blocks_live(self):
        out = rd.build(None, candidate_sha="c" * 40)
        values = json.dumps({k: v for k, v in out.items() if k != "secrets_included"}).upper()
        for bad in ("API_KEY", "API_SECRET", "PASSWORD", "TOKEN", "DATABASE_URL", "POSTGRES://"):
            self.assertNotIn(bad, values)
        self.assertEqual(out["live_status"], "BLOCK")
        self.assertIn("AI_RESEARCH_PROMOTION_BLOCK", out["known_blockers"])
        self.assertEqual(set(out["components"]), {
            "AI_RESEARCH_ARTIFACT_AUTHENTICATED", "AI_EXPECTANCY_AUTHORITY", "AI_UPLIFT_AUTHORITY",
            "AI_CALIBRATION", "AI_COST_STRESS", "AI_PORTFOLIO_ROBUSTNESS", "AI_RESEARCH_PROMOTION"})
        self.assertTrue(all(v == "BLOCK" for v in out["components"].values()))
        self.assertFalse(out["secrets_included"])


def inf_day():
    from bot import nexus_oos_inference as inf
    return inf.DAY_MS


if __name__ == "__main__":
    unittest.main()
