"""Phase 7B: AI research gate, bundle/policy identity, fail-closed selection,
Stage-C AI identity, lifecycle, unified halt authority, runtime modes,
durable journal + restart reconciliation, and replay/runtime feature parity.
"""
import asyncio
import copy
import hashlib
import inspect
import json
import os
import random
import tempfile
import unittest

from bot.ai import ai_gate
from bot.ai import decision as dec
from bot.ai import features as fx
from bot.ai import forward_evidence as fe
from bot.ai import halt as hl
from bot.ai import hook as hk
from bot.ai import identity as aid
from bot.ai import lifecycle as lc
from bot.ai import runtime as rt
from bot.ai import state_machine as smx
from bot.ai import training as tr
from tests.test_ai_decision_authority import (DECISION_TS, M15, H1, H4, candles, constant_bundle,
                                              feature_vector)

T0 = 1_760_054_400_000
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def synthetic_rows(n=2400, seed=5, signal=1.2):
    rng = random.Random(seed)
    out, d = [], len(fx.MODEL_FEATURES)
    for i in range(n):
        f = [rng.gauss(0, 1) for _ in range(d)]
        f[fx.MODEL_FEATURES.index("cost_fraction")] = 0.002
        f[fx.MODEL_FEATURES.index("stop_distance_pct")] = 0.02
        f[fx.MODEL_FEATURES.index("nexus_confidence")] = 60.0
        ts = T0 + i * 3 * H1
        r = signal * f[0] + rng.gauss(0, 0.6)
        out.append({"ts": ts, "outcome_end_ts": ts + H1, "executable": True, "approved": i % 3 == 0,
                    "ai_hook_eligible": True,
                    "outcome_status": "RESOLVED", "r": r, "gross_r": r + 0.1, "ai_features": f,
                    "direction": "LONG" if i % 2 else "SHORT", "symbol": SYMS[i % 5],
                    "ai_regime": ["TREND_UP", "RANGE", "CHOP"][i % 3]})
    return out


_WF = {}


def passing_artifact():
    """Real walk-forward authority aggregates + positive portfolio/cost/concentration."""
    if "wf" not in _WF:
        from bot import nexus_oos_inference as inf
        _WF["wf"] = tr.walk_forward(synthetic_rows(), required_ms=inf.DAY_MS)
    ai = copy.deepcopy(_WF["wf"])
    ai.pop("_fitted", None)
    pooled = ai["pooled_test"]
    pooled["ai_cost_stress"] = {k: {"net_expectancy_r": 0.4} for k in ai_gate.COST_SCENARIOS_REQUIRED}
    pooled["ai_by_month"] = {f"2026-0{m}": {"total_r": 10.0} for m in range(1, 7)}
    pooled["ai_by_symbol"] = {s: {"total_r": 10.0} for s in SYMS}
    fold = {"starting_equity": 1000.0, "ending_equity": 1100.0, "realized_return": 0.1,
            "portfolio_max_drawdown": 0.05, "research_max_drawdown_limit": 0.10,
            "end_state": {"portfolio_censoring_material": False}}
    ai["portfolio_by_test_fold"] = [dict(fold, test_fold=3), dict(fold, test_fold=4)]
    ai["portfolio_kind"] = "AI_HOOK_POLICY_PORTFOLIO"
    return {"candidate_sha": "c" * 40,
            "candidate_research": {"ai_meta_model": ai,
                                   "inference": {"outcome_horizon_resolved_executable": {"max_ms": H1}}}}


def _ai(art):
    return art["candidate_research"]["ai_meta_model"]


class AIResearchGate(unittest.TestCase):
    def test_passing_fixture_passes_every_component(self):
        g = ai_gate.evaluate(passing_artifact())
        self.assertEqual(g["verdict"], "PASS", g["blockers"])
        self.assertTrue(all(v == "PASS" for v in g["components"].values()))

    @staticmethod
    def _shift(section, delta, paired):
        """Shift the TRUE aggregates by delta R per selected trade (all lengths)."""
        for iv in section["block_intervals"]:
            ser = (iv.get("residual_dependence") or {}).get("series")
            if not ser:
                continue
            ks, kn = ("a_sum", "a_count") if paired else ("sum_r", "count")
            ser[ks] = [round(v + delta * c, 12) for v, c in zip(ser[ks], ser[kn])]
        section["mean_r" if not paired else "delta"] = (section.get("mean_r" if not paired else "delta")
                                                        or 0.0) + delta

    def test_forged_positive_stored_ci_does_not_create_pass(self):
        art = passing_artifact()
        auth = _ai(art)["authority"]
        for name, paired in (("ai_approved_expectancy", False), ("ai_uplift_vs_baseline", True)):
            sec = auth[name]
            self._shift(sec, -3.0, paired)                  # true aggregate CI now clearly negative
            for iv in sec["block_intervals"]:
                iv["ci"] = [5.0, 6.0]                       # forged stored CIs
            sec.update({"authority_status": "VALID", "authority_ci_low": 5.0, "authority_ci_high": 6.0})
        g = ai_gate.evaluate(art)
        self.assertEqual(g["verdict"], "BLOCK")
        self.assertEqual(g["components"]["AI_EXPECTANCY_AUTHORITY"], "BLOCK")
        self.assertEqual(g["components"]["AI_UPLIFT_AUTHORITY"], "BLOCK")
        rec = g["recomputed"]["ai_expectancy"]
        self.assertEqual(rec["model"], "AI_AGGREGATE_BLOCK_BOOTSTRAP_V1")
        self.assertLess(rec["ci"][1], 0)
        self.assertFalse(rec["stored_comparison"]["all_interval_cis_match"])

    def test_forged_negative_stored_ci_does_not_destroy_true_positive(self):
        art = passing_artifact()
        truth = ai_gate.evaluate(art)["recomputed"]["ai_expectancy"]["ci"]
        sec = _ai(art)["authority"]["ai_approved_expectancy"]
        for iv in sec["block_intervals"]:
            iv["ci"] = [-6.0, -5.0]
        sec.update({"authority_status": "RESIDUAL_DEPENDENCE_AT_LONGEST_USABLE_BLOCK",
                    "authority_ci_low": -6.0, "authority_ci_high": -5.0})
        sec["residual_dependence_at_longest_usable"]["acf"] = {"1": 0.99, "2": 0.99, "3": 0.99}
        g = ai_gate.evaluate(art)
        self.assertEqual(g["verdict"], "PASS", g["blockers"])
        self.assertEqual(g["recomputed"]["ai_expectancy"]["ci"], truth)
        self.assertGreater(truth[0], 0)

    def test_recomputed_ci_equals_the_replay_bootstrap(self):
        art = passing_artifact()
        g = ai_gate.evaluate(art)
        for rec in g["recomputed"]["ai_expectancy"]["intervals"] + g["recomputed"]["ai_uplift"]["intervals"]:
            if rec["ci"][0] is not None:
                self.assertTrue(rec["stored_matches"], rec)

    def test_zero_variance_series_blocks(self):
        art = passing_artifact()
        sec = _ai(art)["authority"]["ai_approved_expectancy"]
        for iv in sec["block_intervals"]:
            ser = (iv.get("residual_dependence") or {}).get("series")
            if ser:
                mu = sum(ser["sum_r"]) / sum(ser["count"])
                ser["sum_r"] = [mu * c for c in ser["count"]]
        g = ai_gate.evaluate(art)
        self.assertEqual(g["components"]["AI_EXPECTANCY_AUTHORITY"], "BLOCK")

    def test_bootstrap_parameters_are_pinned(self):
        art = passing_artifact()
        _ai(art)["authority"]["ai_approved_expectancy"]["bootstrap"]["samples"] = 100
        self.assertIn("AI_EXPECTANCY_AUTHORITY_BOOTSTRAP_SAMPLES_UNKNOWN", ai_gate.evaluate(art)["blockers"])

    def test_deleting_an_interval_blocks(self):
        art = passing_artifact()
        sec = _ai(art)["authority"]["ai_approved_expectancy"]
        sec["block_intervals"] = [iv for iv in sec["block_intervals"] if iv["block_ms"] != 3 * 86_400_000]
        self.assertIn("AI_EXPECTANCY_AUTHORITY_AGGREGATE_INTERVAL_MISSING", ai_gate.evaluate(art)["blockers"])

    def test_status_label_and_missing(self):
        self.assertIn("AI_META_MODEL_STATUS_MISSING", ai_gate.evaluate({})["blockers"])
        art = passing_artifact()
        _ai(art)["data_label"] = "FRESH"
        self.assertIn("AI_DATA_LABEL_NOT_ACKNOWLEDGED", ai_gate.evaluate(art)["blockers"])

    def test_zero_variance_or_missing_aggregates_block(self):
        art = passing_artifact()
        _ai(art)["authority"]["ai_uplift_vs_baseline"] = {"delta": 1.0, "authority_status": "VALID",
                                                          "authority_ci_low": 1.0}
        g = ai_gate.evaluate(art)
        self.assertEqual(g["components"]["AI_UPLIFT_AUTHORITY"], "BLOCK")

    def test_sample_symbol_period_cost_portfolio_windows_calibration(self):
        cases = {
            "AI_INSUFFICIENT_APPROVED_SAMPLE": lambda a: a["pooled_test"]["ai_approved"].update(n=10),
            "AI_SINGLE_SYMBOL_DOMINATES": lambda a: a["pooled_test"].update(
                ai_by_symbol={"BTCUSDT": {"total_r": 90.0}, "ETHUSDT": {"total_r": 5.0},
                              "SOLUSDT": {"total_r": 5.0}}),
            "AI_TOO_FEW_SYMBOLS_CONTRIBUTING": lambda a: a["pooled_test"].update(
                ai_by_symbol={"BTCUSDT": {"total_r": 50.0}}),
            "AI_SINGLE_PERIOD_DOMINATES": lambda a: a["pooled_test"].update(
                ai_by_month={"2026-01": {"total_r": 90.0}, "2026-02": {"total_r": 10.0}}),
            "AI_COST_STRESS_FAILS_COMBINED_ADVERSE": lambda a: a["pooled_test"]["ai_cost_stress"].update(
                combined_adverse={"net_expectancy_r": -0.1}),
            "AI_PORTFOLIO_NOT_POSITIVE_ON_EVERY_TEST_FOLD": lambda a: a["portfolio_by_test_fold"][1].update(
                ending_equity=900.0, realized_return=-0.1),
            "AI_PORTFOLIO_MISSING": lambda a: a.update(portfolio_by_test_fold=[]),
            "AI_WINDOWS_NOT_PURGED": lambda a: a["steps"][0]["windows"].update(
                validation=[0, 1, 2]),
            "AI_CALIBRATION_DOES_NOT_BEAT_BASE_RATE": lambda a: a["steps"][1]["test_calibration"].update(
                beats_base_rate=False),
        }
        for blocker, mutate in cases.items():
            art = passing_artifact()
            mutate(_ai(art))
            g = ai_gate.evaluate(art)
            self.assertEqual(g["verdict"], "BLOCK", blocker)
            self.assertIn(blocker, g["blockers"])

    def test_censoring_material_blocks_portfolio(self):
        art = passing_artifact()
        _ai(art)["portfolio_by_test_fold"][0]["end_state"]["portfolio_censoring_material"] = True
        self.assertEqual(ai_gate.evaluate(art)["components"]["AI_PORTFOLIO_ROBUSTNESS"], "BLOCK")


class FailClosedSelection(unittest.TestCase):
    def test_insufficient_evidence_disables_and_abstains_all(self):
        rows = synthetic_rows(n=40)
        for r in rows:
            r["ai_features"][fx.MODEL_FEATURES.index("stop_distance_pct")] = 0.02
        p = [0.9] * len(rows)
        er = [2.0] * len(rows)
        pol, rep = tr.select_policy(rows, p, er, probability_authorizes=True)
        self.assertIs(pol, dec.ABSTAIN_ALL)
        self.assertTrue(pol.abstain_all)
        self.assertEqual(rep["policy"], "ABSTAIN_ALL")

    def test_directions_and_regimes_evaluated_at_final_thresholds_no_fallback(self):
        rows = synthetic_rows(n=600, signal=0.0)
        for i, r in enumerate(rows):
            r["r"] = 1.0 if r["direction"] == "LONG" else -1.0
            r["ai_regime"] = "TREND_UP" if (i // 2) % 2 else "EXTREME"
        pol, rep = tr.select_policy(rows, [0.9] * len(rows), [2.0] * len(rows), probability_authorizes=True)
        self.assertEqual(rep["directions"]["SHORT"]["status"], "DISABLED_NEGATIVE")
        self.assertEqual(pol.allowed_directions, ("LONG",))
        self.assertNotIn("EXTREME", pol.supported_regimes)
        self.assertNotIn("UNKNOWN", pol.supported_regimes)
        self.assertEqual(rep["regimes"]["EXTREME"]["status"], "DISABLED_NEVER_TRADABLE")
        self.assertEqual(rep["regimes"]["RANGE"]["status"], "DISABLED_INSUFFICIENT_EVIDENCE")
        # all-negative => nothing enabled => ABSTAIN_ALL (never a LONG fallback)
        for r in rows:
            r["r"] = -1.0
        pol2, _ = tr.select_policy(rows, [0.9] * len(rows), [2.0] * len(rows), probability_authorizes=True)
        self.assertIs(pol2, dec.ABSTAIN_ALL)

    def test_abstain_all_policy_vetoes_every_decision(self):
        b, _, _, _ = constant_bundle(policy=dec.ABSTAIN_ALL)
        d = dec.AIDecisionAuthority(b, pinned_bundle_sha=b.sha256).decide(
            symbol="BTCUSDT", direction="LONG", features=feature_vector(), regime="TREND_UP",
            fees_r=0.0, slippage_r=0.0)
        self.assertIn("POLICY_ABSTAIN_ALL", d.vetoes)

    def test_calibration_that_does_not_beat_base_rate_cannot_authorize(self):
        pol = dec.DecisionPolicy(0.55, 0.05, allowed_directions=("LONG",),
                                 supported_regimes=dec.TRADABLE_REGIMES, probability_authorizes=False)
        b, _, _, _ = constant_bundle(policy=pol)
        d = dec.AIDecisionAuthority(b, pinned_bundle_sha=b.sha256).decide(
            symbol="BTCUSDT", direction="LONG", features=feature_vector(), regime="TREND_UP",
            fees_r=0.0, slippage_r=0.0)
        self.assertIn("CALIBRATION_NOT_AUTHORIZED", d.vetoes)
        self.assertFalse(d.is_trade)

    def test_selection_stability_rule(self):
        s = {"selected_classifier": ["LOGISTIC_L2", {"l2": 1}], "selected_regressor": ["RIDGE", {"l2": 1}],
             "policy": {"allowed_directions": ["LONG"], "min_p_profitable": 0.5, "min_expected_net_r": 0.0}}
        self.assertEqual(tr.selection_stability([s, copy.deepcopy(s)])["status"], "MODEL_SELECTION_STABLE")
        t = copy.deepcopy(s)
        t["selected_classifier"] = ["BOOSTED_STUMPS", {}]
        self.assertEqual(tr.selection_stability([s, t])["status"], "MODEL_SELECTION_UNSTABLE")
        u = copy.deepcopy(s)
        u["policy"]["allowed_directions"] = []
        self.assertEqual(tr.selection_stability([u, u])["status"], "MODEL_SELECTION_UNSTABLE")


class FeatureSchemaAndIdentity(unittest.TestCase):
    def test_zero_with_flag_companions(self):
        self.assertEqual(fx.FEATURE_SCHEMA_VERSION, "NEXUS7_AI_FEATURES_V3")
        for n in fx.FLAGGED_FEATURES:
            self.assertIn(f"{n}__missing", fx.MODEL_FEATURES)
        fv = feature_vector(nexus_confidence=None)
        x = dict(zip(fx.MODEL_FEATURES, fv.model_input()))
        self.assertEqual(x["nexus_confidence"], 0.0)
        self.assertEqual(x["nexus_confidence__missing"], 1.0)
        self.assertEqual(dict(zip(fx.MODEL_FEATURES, feature_vector().model_input()))
                         ["nexus_confidence__missing"], 0.0)

    def test_decision_id_binds_bundle_policy_schema_features_candidate(self):
        base = dict(symbol="BTCUSDT", ts=1, direction="LONG", feature_hash="f", feature_schema_sha256="s",
                    bundle_sha256="b", policy_sha256="p", candidate_sha="c")
        ref = dec.decision_id(**base)
        self.assertEqual(ref, dec.decision_id(**base))
        for k in ("feature_hash", "feature_schema_sha256", "bundle_sha256", "policy_sha256", "candidate_sha"):
            self.assertNotEqual(ref, dec.decision_id(**{**base, k: "other"}), k)
        from bot.ai import authority_chain as ac
        self.assertNotEqual(ac.client_oid(ref), ac.client_oid(dec.decision_id(**{**base, "policy_sha256": "q"})))

    def test_bundle_sha_covers_every_decision_affecting_part(self):
        _, ca, ra, man = constant_bundle()
        for field, value in (("calibration", {"kind": "PLATT", "a": 1.0, "b": 0.0}),
                             ("decision_policy", {**man["decision_policy"], "min_p_profitable": 0.1}),
                             ("feature_schema_sha256", "0" * 64), ("ai_version", "X"),
                             ("training_code_sha", "z" * 40), ("training_dataset_manifest_sha256", "e" * 64),
                             ("training_period", {"first_ts": 1}), ("selection_evidence", {"x": 1}),
                             ("created_at", "2027-01-01T00:00:00Z")):
            t = dict(man, **{field: value})
            self.assertNotEqual(dec.bundle_sha(t), man["bundle_sha256"], field)
            with self.assertRaises(Exception):
                dec.ModelBundle.load(json.dumps(t), json.dumps(ca), json.dumps(ra),
                                     pinned_bundle_sha=man["bundle_sha256"])

    def test_policy_hash_is_verified(self):
        _, ca, ra, man = constant_bundle()
        t = dict(man, decision_policy={**man["decision_policy"], "min_p_profitable": 0.1})
        t["bundle_sha256"] = dec.bundle_sha(t)
        with self.assertRaisesRegex(Exception, "POLICY_HASH_MISMATCH"):
            dec.ModelBundle.load(json.dumps(t), json.dumps(ca), json.dumps(ra), pinned_bundle_sha=t["bundle_sha256"])

    def test_cost_contract_named_quantities_and_training_parity(self):
        c = dec.cost_contract(predicted_gross_r=1.0, fees_r=0.1, conservative_slippage_buffer_r=0.05,
                              funding_r=0.0, decision_uncertainty_buffer_r=0.05)
        self.assertIn("CONSERVATIVE_SLIPPAGE_BUFFER_r", c)
        self.assertAlmostEqual(c["predicted_net_r"], 0.8)
        costs = rt.runtime_costs(cost_fraction=0.0022, stop_distance_pct=0.01, taker_fee=0.0006)
        self.assertAlmostEqual(costs["fees_r"] + costs["slippage_r"], tr.cost_estimate_r(0.0022, 0.01))


def closed_window(ks, tf, ts):
    return [c for c in ks if c["ts"] + tf <= ts]


class GoldenFeatureParity(unittest.TestCase):
    """Replay (closed windows) and runtime (live cache incl. forming candle)
    produce the SAME model input, feature hash and regime."""

    def _candles(self):
        k15 = candles(120, M15, DECISION_TS)
        k1h = candles(60, H1, DECISION_TS, seed=2)
        k4h = candles(40, H4, DECISION_TS, seed=3)
        forming = {"ts": DECISION_TS, "o": 1.0, "h": 9.0, "l": 0.5, "c": 7.0, "v": 1.0}
        return k15, k1h, k4h, forming

    def test_replay_vs_runtime(self):
        k15, k1h, k4h, forming = self._candles()
        kw = dict(decision_ts=DECISION_TS, direction="LONG", strategy_score=72, entry=100.0, stop=99.0,
                  rr=2.5, cost_fraction=0.0022, nexus_confidence=65.0)
        replay_fv, replay_reg = fx.candidate_features(
            closed_window(k15, M15, DECISION_TS), closed_window(k1h, H1, DECISION_TS),
            closed_window(k4h, H4, DECISION_TS), **kw)
        live_fv, live_reg = fx.candidate_features(k15 + [forming], k1h + [dict(forming)],
                                                  k4h + [dict(forming)], **kw)
        self.assertEqual(replay_fv.model_input(), live_fv.model_input())
        self.assertEqual(replay_fv.feature_hash(), live_fv.feature_hash())
        self.assertEqual(replay_reg, live_reg)
        golden = hashlib.sha256(json.dumps([round(x, 9) for x in replay_fv.model_input()]).encode()).hexdigest()
        self.assertEqual(golden, GOLDEN_MODEL_INPUT_SHA256)

    def test_runtime_gate_uses_the_same_features(self):
        k15, k1h, k4h, forming = self._candles()
        r, journal = runtime_with_bundle("SHADOW")
        out = run(gate_call(r, symbol="BTCUSDT", direction="LONG", entry=100.0, stop=99.0, rr=2.5,
                            strategy_score=72, nexus_confidence=65.0, cost_fraction=0.0022, taker_fee=0.0006,
                            k15=k15 + [forming], k1h=k1h, k4h=k4h, now_ms=DECISION_TS + 60_000))
        replay_fv, _ = fx.candidate_features(k15, k1h, k4h, decision_ts=DECISION_TS, direction="LONG",
                                             strategy_score=72, entry=100.0, stop=99.0, rr=2.5,
                                             cost_fraction=0.0022, nexus_confidence=65.0)
        self.assertEqual(out.decision.feature_hash, replay_fv.feature_hash())
        self.assertEqual(out.decision.timestamp, DECISION_TS)


GOLDEN_MODEL_INPUT_SHA256 = "e61f05e844077aa51f10b6e7b12aec3f39e5ca2e5bf313445a318784a3ecfa27"


# ── runtime fixtures ──────────────────────────────────────────────────────
class FakeDB:
    def __init__(self, fail=False):
        self.rows, self.fail = {}, fail

    async def save_ai_decision(self, decision_id, client_oid, ts, symbol, side, status, bsha, psha, rec, *,
                               strict=True):
        if self.fail:
            from bot.database import PersistenceError
            raise PersistenceError("down")
        prev = self.rows.get(decision_id, {})
        self.rows[decision_id] = {"client_oid": client_oid or prev.get("client_oid"), "status": status,
                                  "record": json.loads(rec)}
        return True

    async def load_ai_decision(self, decision_id, *, strict=True):
        r = self.rows.get(decision_id)
        return (r["status"], r["record"]) if r else None

    async def load_open_ai_decisions(self, *, strict=True):
        return [{"decision_id": k, "client_oid": v["client_oid"], "status": v["status"], "record": v["record"]}
                for k, v in self.rows.items() if v["status"] in ("INTENT_CREATED", "SUBMITTED", "PENDING_UNKNOWN")]


def write_bundle(tmp, lifecycle="SHADOW_CHALLENGER", p=0.62, hook_profile=None, gross_r=0.5):
    b, ca, ra, man = constant_bundle(p=p, gross_r=gross_r, lifecycle=lifecycle, hook_profile=hook_profile)
    for name, obj in (("manifest.json", man), ("classifier.json", ca), ("regressor.json", ra)):
        with open(os.path.join(tmp, name), "w") as fh:
            json.dump(obj, fh)
    return man["bundle_sha256"]


def runtime_with_bundle(mode, *, lifecycle=None, paper_trade=None, pin=None, db=None, stage_c=None, p=0.62,
                        bundle_profile=None, engine_profile=None, gross_r=0.5):
    """PAPER engines run the pre-geometry hook profile; LIVE pilot the post-geometry one."""
    tmp = tempfile.mkdtemp()
    lifecycle = lifecycle or {"SHADOW": "SHADOW_CHALLENGER", "PAPER": "PAPER_CHALLENGER",
                              "LIVE": "LIVE_CHAMPION"}.get(mode, "SHADOW_CHALLENGER")
    default_profile = hk.PROFILE_PRE_GEOMETRY if mode == "PAPER" else hk.PROFILE_LIVE_PILOT
    bundle_profile = bundle_profile or default_profile
    engine_profile = engine_profile or default_profile
    sha = write_bundle(tmp, lifecycle, p=p, hook_profile=bundle_profile, gross_r=gross_r)
    env = {"AI_EXECUTION_MODE": mode, "AI_BUNDLE_DIR": tmp, "AI_BUNDLE_SHA256": pin or sha}
    journal = rt.DurableAIJournal(db or FakeDB())
    r = rt.AIRuntime(env, journal=journal, paper_trade=(mode == "PAPER") if paper_trade is None else paper_trade,
                     clock_ms=lambda: DECISION_TS + 60_000)
    r.startup(candidate_sha="c" * 40, stage_c_result=stage_c, hook_profile=engine_profile)
    return r, journal


def gate_call(r, *, symbol, direction, entry, stop, rr, strategy_score, nexus_confidence, cost_fraction,
              taker_fee, k15, k1h, k4h, now_ms, profile=None):
    obs = hk.HookObservation(symbol=symbol, direction=direction, decision_ts=rt.AIRuntime.event_ts(now_ms),
                             entry=entry, stop=stop, rr=rr, strategy_score=strategy_score,
                             nexus_confidence=nexus_confidence, cost_fraction=cost_fraction,
                             profile=profile or r.hook_profile or hk.PROFILE_LIVE_PILOT)
    return r.gate(obs, k15, k1h, k4h, taker_fee=taker_fee, now_ms=now_ms)


def gate_kwargs(**over):
    k15 = candles(120, M15, DECISION_TS)
    kw = dict(symbol="BTCUSDT", direction="LONG", entry=100.0, stop=99.0, rr=2.5, strategy_score=72,
              nexus_confidence=65.0, cost_fraction=0.0022, taker_fee=0.0006, k15=k15,
              k1h=candles(60, H1, DECISION_TS, seed=2), k4h=candles(40, H4, DECISION_TS, seed=3),
              now_ms=DECISION_TS + 60_000)
    kw.update(over)
    return kw


class RuntimeModes(unittest.TestCase):
    def test_off_is_default_and_a_no_op(self):
        r = rt.AIRuntime({})
        self.assertEqual(r.mode, "OFF")
        r.startup()
        out = run(gate_call(r, **gate_kwargs()))
        self.assertEqual((out.allow, out.authoritative, out.decision), (True, False, None))

    def test_invalid_mode_halts(self):
        r = rt.AIRuntime({"AI_EXECUTION_MODE": "LIVEE"})
        r.startup()
        self.assertTrue(r.halts.halted)

    def test_shadow_decides_journals_and_never_blocks(self):
        r, j = runtime_with_bundle("SHADOW")
        out = run(gate_call(r, **gate_kwargs()))
        self.assertTrue(out.allow)
        self.assertFalse(out.authoritative)
        self.assertIsNotNone(out.decision)
        self.assertEqual(j.db.rows[out.decision.decision_id]["status"], "SHADOW_TRADE")
        r2, _ = runtime_with_bundle("SHADOW", p=0.3)
        out2 = run(gate_call(r2, **gate_kwargs()))
        self.assertTrue(out2.allow)                        # AI abstains but has no authority in SHADOW
        self.assertEqual(out2.reason, "SHADOW_ABSTAIN")
        src = inspect.getsource(rt)
        for word in ("place_order", "send_order", "create_order", ".post("):
            self.assertNotIn(word, src)

    def test_paper_is_mandatory_authorization(self):
        r, j = runtime_with_bundle("PAPER")
        run(r.recover(lambda oid: asyncio.sleep(0, "FOUND")))
        ok = run(gate_call(r, **gate_kwargs()))
        self.assertTrue(ok.allow and ok.authoritative, ok.reason)
        r2, _ = runtime_with_bundle("PAPER", p=0.3)
        run(r2.recover(lambda oid: asyncio.sleep(0, "FOUND")))
        no = run(gate_call(r2, **gate_kwargs()))
        self.assertFalse(no.allow)
        self.assertTrue(no.reason.startswith("AI_ABSTAIN"))

    def test_paper_on_live_engine_halts(self):
        r, _ = runtime_with_bundle("PAPER", paper_trade=False)
        self.assertIn("STAGE_C_EVIDENCE_INVALID", r.halts.active)

    def test_live_without_stage_c_or_ai_identity_halts(self):
        r, _ = runtime_with_bundle("LIVE")
        self.assertIn("STAGE_C_EVIDENCE_INVALID", r.halts.active)
        run(r.recover(lambda oid: asyncio.sleep(0, "FOUND")))
        self.assertFalse(run(gate_call(r, **gate_kwargs())).allow)
        stage_ok_no_ai = {"gate": "LIVE_RELEASE_GATE", "verdict": "PASS", "live_provenance_authenticated": True,
                          "production_ready": True,
                          "stages": {"AI_IDENTITY": "BLOCK", "release_authority_kind": "AI_LIVE"}}
        self.assertEqual(rt.authorize_ai_live(stage_ok_no_ai), ["STAGE_C_AI_IDENTITY_NOT_PASSED"])
        full = dict(stage_ok_no_ai, stages={"AI_IDENTITY": "PASS", "release_authority_kind": "AI_LIVE"})
        nexus_only = dict(stage_ok_no_ai, stages={"AI_IDENTITY": "PASS", "release_authority_kind": "NEXUS_ONLY"})
        self.assertEqual(rt.authorize_ai_live(nexus_only), ["STAGE_C_NOT_AI_LIVE_RELEASE"])
        self.assertEqual(rt.authorize_ai_live(full), [])
        r3, _ = runtime_with_bundle("LIVE", stage_c=full)
        self.assertFalse(r3.halts.halted, r3.halts.active)

    def test_lifecycle_state_restricts_mode(self):
        r, _ = runtime_with_bundle("PAPER", lifecycle="SHADOW_CHALLENGER")
        self.assertIn("MODEL_ARTIFACT_MISMATCH", r.halts.active)

    def test_startup_model_mismatch_halts_before_any_entry(self):
        r, _ = runtime_with_bundle("PAPER", pin="0" * 64)
        self.assertIn("MODEL_ARTIFACT_MISMATCH", r.halts.active)
        out = run(gate_call(r, **gate_kwargs()))
        self.assertFalse(out.allow)
        missing = rt.AIRuntime({"AI_EXECUTION_MODE": "PAPER"}, journal=rt.DurableAIJournal(FakeDB()),
                               paper_trade=True)
        missing.startup()
        self.assertIn("MODEL_ARTIFACT_MISMATCH", missing.halts.active)

    def test_journal_write_failure_blocks_authoritative_trade(self):
        r, _ = runtime_with_bundle("PAPER", db=FakeDB(fail=True))
        r.recovered = True
        out = run(gate_call(r, **gate_kwargs()))
        self.assertFalse(out.allow)
        self.assertTrue(out.reason.startswith("AI_JOURNAL"))

    def test_recovery_required_before_new_authoritative_entry(self):
        r, _ = runtime_with_bundle("PAPER")
        self.assertEqual(run(gate_call(r, **gate_kwargs())).reason, "AI_RECOVERY_NOT_COMPLETE")

    def test_same_market_event_is_never_reordered(self):
        r, _ = runtime_with_bundle("PAPER")
        run(r.recover(lambda oid: asyncio.sleep(0, "FOUND")))
        first = run(gate_call(r, **gate_kwargs()))
        self.assertTrue(first.allow)
        again = run(gate_call(r, **gate_kwargs(now_ms=DECISION_TS + 120_000)))
        self.assertFalse(again.allow)
        self.assertEqual(again.reason, "DUPLICATE_MARKET_EVENT")

    def test_observation_line_exposes_identity_only(self):
        r, _ = runtime_with_bundle("SHADOW")
        line = r.observation_line(candidate_sha="c" * 40, deployment_id="dep-A")
        obs = aid.parse(line)
        self.assertEqual(obs["bundle_sha256"], r.bundle.sha256)
        self.assertEqual(obs["policy_sha256"], r.bundle.policy.sha256)
        self.assertEqual(obs["feature_schema_sha256"], fx.schema_hash())
        self.assertNotIn("min_p_profitable", line)
        self.assertNotIn('"w"', line)


class DurableJournalRestart(unittest.TestCase):
    def test_restart_reconciles_pending_client_oid_without_resubmitting(self):
        db = FakeDB()
        r, _ = runtime_with_bundle("PAPER", db=db)
        run(r.recover(lambda oid: asyncio.sleep(0, "FOUND")))
        out = run(gate_call(r, **gate_kwargs()))
        run(r.bind_intent(out.decision, "bgx7-abc"))
        run(r.mark(out.decision, "PENDING_UNKNOWN"))
        # restart: new runtime, same durable store
        r2, _ = runtime_with_bundle("PAPER", db=db)
        looked = []

        async def lookup(oid):
            looked.append(oid)
            return "FOUND"
        res = run(r2.recover(lookup))
        self.assertEqual((res["open"], res["found"]), (1, 1))
        self.assertEqual(looked, ["bgx7-abc"])
        self.assertEqual(db.rows[out.decision.decision_id]["status"], "RECONCILED_FOUND")
        again = run(gate_call(r2, **gate_kwargs()))
        self.assertFalse(again.allow)
        self.assertEqual(again.reason, "DUPLICATE_MARKET_EVENT")

    def test_uncertain_reconciliation_halts(self):
        db = FakeDB()
        r, _ = runtime_with_bundle("PAPER", db=db)
        run(r.recover(lambda oid: asyncio.sleep(0, "FOUND")))
        out = run(gate_call(r, **gate_kwargs()))
        run(r.bind_intent(out.decision, "bgx7-xyz"))
        r2, _ = runtime_with_bundle("PAPER", db=db)

        async def boom(oid):
            raise ConnectionError
        run(r2.recover(boom))
        self.assertIn("RECONCILIATION_UNCERTAIN", r2.halts.active)
        self.assertFalse(run(gate_call(r2, **gate_kwargs(now_ms=DECISION_TS + 20 * M15))).allow)

    def test_journal_survives_process_restart_in_sqlite(self):
        from bot import database as db
        saved = (db.DATABASE_URL, db.SQLITE_PATH, db._conn, db._is_pg)
        tmp = tempfile.mkdtemp()
        try:
            db.DATABASE_URL, db.SQLITE_PATH, db._conn, db._is_pg = "", os.path.join(tmp, "t.db"), None, False

            async def first():
                await db.init()
                r, _ = runtime_with_bundle("PAPER", db=db)
                await r.recover(lambda oid: asyncio.sleep(0, "FOUND"))
                out = await gate_call(r, **gate_kwargs())
                await r.bind_intent(out.decision, "bgx7-sqlite")
                await db._conn.close()
                db._conn = None
                return out.decision.decision_id

            async def second():
                await db.init()
                rows = await db.load_open_ai_decisions()
                await db._conn.close()
                db._conn = None
                return rows
            loop = asyncio.new_event_loop()
            did = loop.run_until_complete(first())
            rows = loop.run_until_complete(second())
            self.assertEqual([(x["decision_id"], x["client_oid"], x["status"]) for x in rows],
                             [(did, "bgx7-sqlite", "INTENT_CREATED")])
            self.assertEqual(rows[0]["record"]["decision"]["decision_id"], did)
        finally:
            db.DATABASE_URL, db.SQLITE_PATH, db._conn, db._is_pg = saved


class HaltAuthority(unittest.TestCase):
    CHECKS = {"exchange_reconciled": True, "positions_protected": True, "model_verified": True, "clock_ok": True}

    def test_every_halt_source_blocks_and_only_operator_recovers(self):
        for cond in ("RECONCILIATION_UNCERTAIN", "UNPROTECTED_POSITION", "MODEL_ARTIFACT_MISMATCH",
                     "FEATURE_SCHEMA_MISMATCH", "STAGE_C_EVIDENCE_INVALID", "CLOCK_ANOMALY"):
            m = smx.AutonomousStateMachine(state="WAIT_FOR_DATA")
            m.halt(cond, source="TEST")
            self.assertEqual(m.state, "HALT")
            self.assertIn(cond, m.halts.active)
            with self.assertRaises(smx.RecoveryRefused):
                m.go("SYNC_EXCHANGE")
            with self.assertRaises(hl.HaltClearRefused):
                m.operator_recover(actor="AI", reason="confident", checks=self.CHECKS)
            with self.assertRaises(hl.HaltClearRefused):
                m.operator_recover(actor=hl.OPERATOR, reason=" ", checks=self.CHECKS)
            with self.assertRaises(smx.RecoveryRefused):
                m.operator_recover(actor=hl.OPERATOR, reason="checked",
                                   checks={**self.CHECKS, "positions_protected": False})
            self.assertEqual(m.state, "HALT")
            m.operator_recover(actor=hl.OPERATOR, reason="verified on exchange", checks=self.CHECKS)
            self.assertEqual(m.state, "RECOVER")
            self.assertFalse(m.halts.halted)

    def test_go_halt_registers_condition_and_runtime_halts_block_entries(self):
        m = smx.AutonomousStateMachine(state="WAIT_FOR_DATA")
        m.go("HALT", reason="CLOCK_ANOMALY")
        self.assertIn("CLOCK_ANOMALY", m.halts.active)
        r, _ = runtime_with_bundle("PAPER")
        run(r.recover(lambda oid: asyncio.sleep(0, "FOUND")))
        r.halts.raise_halt("FEATURE_SCHEMA_MISMATCH", source="TEST")
        self.assertFalse(run(gate_call(r, **gate_kwargs())).allow)

    def test_recover_is_journaled(self):
        from bot.ai import journal as jr
        m = smx.AutonomousStateMachine(state="WAIT_FOR_DATA", journal=jr.DecisionJournal())
        m.halt("UNPROTECTED_POSITION", source="TEST")
        m.operator_recover(actor=hl.OPERATOR, reason="protected", checks=self.CHECKS)
        self.assertEqual(m.journal.records[-1]["decision"]["event"], "OPERATOR_RECOVER")


class StageCAIIdentity(unittest.TestCase):
    SHA = "a" * 40

    def _identity(self, **over):
        b, _, _, _ = constant_bundle(lifecycle="LIVE_CHAMPION")
        ident = {"candidate_sha": self.SHA, "ai_version": dec.AI_VERSION, "bundle_sha256": b.sha256,
                 "policy_sha256": b.policy.sha256, "feature_schema_sha256": fx.schema_hash()}
        ident.update(over)
        return ident

    def _sources(self, approved, runtime_ident, mode="LIVE", at=None):
        import datetime as dt
        line = aid.observation_line(candidate_sha=runtime_ident["candidate_sha"], deployment_id="dep-A", mode=mode,
                                    ai_version=runtime_ident["ai_version"],
                                    bundle_sha256=runtime_ident["bundle_sha256"],
                                    policy_sha256=runtime_ident["policy_sha256"],
                                    feature_schema_sha256=runtime_ident["feature_schema_sha256"],
                                    now=at or dt.datetime.now(dt.timezone.utc))

        class S:
            source_kind = "READ_ONLY_CONTROL_PLANE"

            def approved_ai_identity(self, sha):
                return approved

            def railway_deployment_logs(self, dep):
                return [line]
        return S()

    def _env(self, claim):
        return {"candidate_sha": self.SHA, "railway": {"deployment_id": "dep-A"}, "ai_identity": claim}

    def test_exact_identity_passes(self):
        ident = self._identity()
        out = aid.verify_ai_identity(self._env(ident), sources=self._sources(
            {**ident, "lifecycle_state": "LIVE_CHAMPION"}, ident))
        self.assertEqual(out["verdict"], "PASS", out["blockers"])

    def test_wrong_model_or_policy_with_correct_code_sha_blocks(self):
        ident = self._identity()
        approved = {**ident, "lifecycle_state": "LIVE_CHAMPION"}
        for field in ("bundle_sha256", "policy_sha256", "feature_schema_sha256"):
            wrong = dict(ident, **{field: "9" * 64})
            out = aid.verify_ai_identity(self._env(ident), sources=self._sources(approved, wrong))
            self.assertEqual(out["verdict"], "BLOCK", field)
            self.assertIn(f"AI_RUNTIME_{field.upper()}_MISMATCH", out["blockers"])
            out2 = aid.verify_ai_identity(self._env(wrong), sources=self._sources(approved, ident))
            self.assertIn(f"AI_IDENTITY_CLAIM_DIFFERS_FROM_APPROVED_{field.upper()}", out2["blockers"])

    def test_missing_unknown_unapproved_stale_block(self):
        ident = self._identity()
        approved = {**ident, "lifecycle_state": "LIVE_CHAMPION"}
        self.assertIn("AI_IDENTITY_MISSING",
                      aid.verify_ai_identity({"candidate_sha": self.SHA}, sources=None)["blockers"])
        self.assertIn("AI_IDENTITY_SOURCE_UNAVAILABLE", aid.verify_ai_identity(self._env(ident), sources=None)["blockers"])
        shadow = aid.verify_ai_identity(self._env(ident), sources=self._sources(
            {**ident, "lifecycle_state": "SHADOW_CHALLENGER"}, ident))
        self.assertIn("AI_IDENTITY_NOT_APPROVED_FOR_LIVE", shadow["blockers"])
        unknown = aid.verify_ai_identity(self._env(dict(ident, bundle_sha256="UNAVAILABLE")),
                                         sources=self._sources(approved, ident))
        self.assertIn("AI_IDENTITY_UNKNOWN_BUNDLE_SHA256", unknown["blockers"])
        not_live = aid.verify_ai_identity(self._env(ident), sources=self._sources(approved, ident, mode="SHADOW"))
        self.assertIn("AI_RUNTIME_MODE_NOT_LIVE", not_live["blockers"])
        import datetime as dt
        old = aid.verify_ai_identity(self._env(ident), sources=self._sources(
            approved, ident, at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)))
        self.assertIn("AI_IDENTITY_OBSERVATION_STALE", old["blockers"])
        other_code = aid.verify_ai_identity(self._env(ident), sources=self._sources(
            dict(approved, candidate_sha="b" * 40), ident))
        self.assertIn("AI_IDENTITY_APPROVED_FOR_OTHER_CODE", other_code["blockers"])

    def test_live_gate_reports_ai_identity_stage(self):
        from bot import nexus_oos_promotion_gate as gate
        d = gate.evaluate_live({}, None).to_dict()
        self.assertEqual(d["stages"]["AI_IDENTITY"], "BLOCK")
        self.assertIn("approved_ai_identity", __import__("bot.live_release_evidence", fromlist=["x"]).READ_METHODS)


class LifecycleAndForward(unittest.TestCase):
    def test_no_skipping_and_evidence_required(self):
        with self.assertRaises(lc.PromotionRefused):
            lc.promote("RESEARCH_CANDIDATE", "LIVE_CHAMPION", {})
        with self.assertRaises(lc.PromotionRefused):
            lc.promote("SHADOW_CHALLENGER", "PAPER_CHALLENGER", {"FORWARD_SHADOW_EVIDENCE": "INSUFFICIENT_EVIDENCE"})
        self.assertEqual(lc.promote("RESEARCH_CANDIDATE", "SHADOW_CHALLENGER",
                                    {"AI_RESEARCH_PROMOTION_GATE": "PASS", "MODEL_SELECTION_STABLE": True}),
                         "SHADOW_CHALLENGER")
        self.assertFalse(lc.mode_allowed("SHADOW_CHALLENGER", "LIVE"))
        self.assertFalse(lc.mode_allowed("PAPER_CHALLENGER", "LIVE"))

    def test_forward_contracts_are_predeclared_and_insufficient(self):
        for mode in ("SHADOW", "PAPER"):
            s = fe.status(None, mode)
            self.assertEqual(s["verdict"], "INSUFFICIENT_EVIDENCE")
            self.assertEqual(len(s["contract_sha256"]), 64)
        self.assertGreaterEqual(fe.SHADOW_CONTRACT["min_calendar_days"], 30)

    def test_shadow_challenger_bundle_loads_through_runtime_validation(self):
        passing_artifact()
        wf = _WF["wf"]
        sc = tr.build_shadow_challenger(synthetic_rows(), wf, training_code_sha="c" * 40,
                                        created_at="2026-09-24T00:00:00Z")
        self.assertEqual((sc["created"], sc["lifecycle_state"]), (True, "SHADOW_CHALLENGER"))
        man = sc["bundle"]
        self.assertEqual(man["lifecycle_state"], "SHADOW_CHALLENGER")
        b = dec.ModelBundle.load(json.dumps(man), json.dumps(sc["classifier_artifact"]),
                                 json.dumps(sc["regressor_artifact"]), pinned_bundle_sha=sc["bundle_sha256"])
        self.assertEqual(b.policy.sha256, sc["decision_policy_sha256"])
        self.assertFalse(lc.mode_allowed(man["lifecycle_state"], "PAPER"))

    def test_shadow_challenger_only_on_pass_and_stable(self):
        src = inspect.getsource(__import__("bot.nexus_oos_real_replay", fromlist=["x"])._ai_portfolio_and_gate)
        self.assertIn('gate["verdict"] == "PASS" and stable', src)
        self.assertNotIn("LIVE_CHAMPION", src)
        self.assertNotIn('"LIVE_CHAMPION"', inspect.getsource(tr.build_shadow_challenger))


class EngineIntegration(unittest.TestCase):
    def test_ai_gate_sits_after_nexus_before_balance_sizing_and_order(self):
        from bot.engine import TradingEngine
        src = inspect.getsource(TradingEngine._open)
        i_nexus = src.index("if not approved:")
        i_ai = src.index("self._ai_gate(sig, nx_dec)")
        i_bal = src.index("_refresh_entry_balance()")
        i_oid = src.index("build_client_oid(")
        i_bind = src.index("self.ai_runtime.bind_intent(")
        i_send = src.index("self.client.place_order(")
        self.assertLess(i_nexus, i_ai)
        self.assertLess(i_ai, i_bal)
        self.assertLess(i_oid, i_bind)
        self.assertLess(i_bind, i_send)
        self.assertIn("_idem = _ai_dec.decision_id", src)
        self.assertEqual(src.count("self.client.place_order("), 2)   # entry + existing fail-safe close only

    def test_default_engine_ai_mode_is_off(self):
        self.assertEqual(rt.resolve_ai_mode({}), "OFF")
        from bot.engine import TradingEngine
        self.assertIn("await self._ai_startup()", inspect.getsource(TradingEngine.run))


if __name__ == "__main__":
    unittest.main()
