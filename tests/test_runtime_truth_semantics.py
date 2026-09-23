import asyncio
import copy
import unittest
from dataclasses import dataclass

from bot import runtime_truth as truth
from bot import runtime_truth_hooks as hooks


@dataclass
class FakeSignal:
    symbol: str = "LTCUSDT"
    direction: str = "SHORT"
    score: int = 76
    entry_type: str = "MOMENTUM"
    regime: str = "TRENDING_DOWN"
    entry: float = 100.0
    sl: float = 102.0
    tp: float = 96.0


@dataclass
class FakeDecision:
    execution_allowed: bool = True
    decision: str = "APPROVE"
    confidence: float = 0.91
    setup_quality: int = 88


class FakeAnalyzer:
    def analyze_mtf(self, symbol, k15, k1h, k4h, *args, **kwargs):
        return FakeSignal(symbol=symbol)


class FakeScoring:
    async def calculate(self, symbol, direction, closes, highs, lows, volumes, client=None):
        return {"aprovado": True, "total": 91, "tecnico": 90, "orderflow": 92}


class FakeNexus:
    def decide(self, *args, **kwargs):
        return FakeDecision()


class FakeClient:
    async def place_order(self, *args, **kwargs):
        return {"orderId": "paper-safe", "clientOid": "truth-test"}
    async def wait_for_fill(self, order_id, *args, **kwargs):
        return {"filled": True, "timed_out": False, "status": {"id": order_id}}


class FakeEngine:
    def __init__(self, scoring, nexus):
        self.client = FakeClient()
        self.scoring = scoring
        self.nexus = nexus
    async def run(self):
        return "run-result"
    async def _nexus_validate(self, sig):
        rows = make_rows(25, 900000)
        return await asyncio.to_thread(
            self.nexus.decide, symbol=sig.symbol,
            k15=rows, k1h=make_rows(20, 3600000), k4h=make_rows(12, 14400000),
            entry=sig.entry, sl=sig.sl, tp=sig.tp,
        )
    async def _open(self, sig):
        nx = await self._nexus_validate(sig)
        pre = await self.scoring.calculate(sig.symbol, sig.direction, [1.0]*25, [2.0]*25, [0.5]*25, [10.0]*25, self.client)
        order = await self.client.place_order(symbol=sig.symbol, side="Sell", qty=1.0)
        fill = await self.client.wait_for_fill(order["orderId"])
        return {"nexus": nx.__dict__, "pre": pre, "order": order, "fill": fill}


def make_rows(count, width):
    base = 1790000000000
    return [
        {"ts":base+i*width,"o":100.0+i,"h":101.0+i,"l":99.0+i,"c":100.5+i,"v":1000.0+i}
        for i in range(count)
    ]


class RuntimeTruthSemanticTests(unittest.TestCase):
    def setUp(self):
        self.old_enabled = truth._ENABLED
        self.old_recorder = truth.RECORDER
        truth._ENABLED = True
        truth.RECORDER = truth.Recorder()
        truth.RECORDER.enabled = True
        hooks._SIGNAL_EVALUATIONS.clear()

    def tearDown(self):
        truth._ENABLED = self.old_enabled
        truth.RECORDER = self.old_recorder

    def test_stage_wrappers_return_exact_same_signal_object(self):
        analyzer = FakeAnalyzer()
        cls = type("AnalyzerCase", (FakeAnalyzer,), {})
        instance = cls()
        original_result = instance.analyze_mtf("LTCUSDT", [], [], [])
        hooks.install_canonical_stage(cls)
        hooks.install_adaptive_stage(cls)
        hooks.install_pullback_stage(cls)
        raw = {"15":make_rows(25,900000),"60":make_rows(20,3600000),"240":make_rows(12,14400000)}
        meta = {"_raw_inputs":raw}
        eval_id = truth.new_evaluation_id("POLICY_A_STRATEGY","LTCUSDT")
        with truth.evaluation_context(eval_id,"POLICY_A_STRATEGY",meta):
            result = instance.analyze_mtf("LTCUSDT", raw["15"], raw["60"], raw["240"])
        self.assertEqual(result, original_result)
        events = truth.RECORDER.drain(100, 1_000_000)
        stages = [e["payload"].get("stage") for e in events if e["event_type"] == "MARKET_ANALYSIS_RESULT"]
        self.assertEqual(stages, ["CANONICAL_ANALYZER_RESULT","ADAPTIVE_MTF_RESULT","PULLBACK_CONFIRMATION_RESULT"])
        authorities = [e["payload"].get("authority") for e in events if e["event_type"] == "MARKET_ANALYSIS_RESULT"]
        self.assertEqual(authorities, ["STRATEGY_SIGNAL","STRATEGY_SIGNAL","POST_CONFIRMATION_SIGNAL"])
        self.assertNotIn("EXECUTABLE", authorities)

    def test_analysis_authority_records_raw_and_prepared_hashes(self):
        cls = type("AnalyzerInputCase", (FakeAnalyzer,), {})
        hooks.install_analysis_authority_inner(cls)
        instance = cls()
        raw = {"15":make_rows(25,900000),"60":make_rows(20,3600000),"240":make_rows(12,14400000)}
        prepared = {tf:list(rows) for tf,rows in raw.items()}
        eval_id = truth.new_evaluation_id("POLICY_A_STRATEGY","LTCUSDT")
        with truth.evaluation_context(eval_id,"POLICY_A_STRATEGY",{"_raw_inputs":raw}):
            instance.analyze_mtf("LTCUSDT",prepared["15"],prepared["60"],prepared["240"])
        event = next(e for e in truth.RECORDER.drain(100,1_000_000) if e["event_type"]=="MARKET_ANALYSIS_INPUT")
        for tf in ("15","60","240"):
            self.assertEqual(event["payload"]["timeframes"][tf]["ANALYZER_RAW_INPUT_HASH"], truth.cache_data_hash(raw[tf]))
            self.assertEqual(event["payload"]["timeframes"][tf]["ANALYZER_PREPARED_INPUT_HASH"], truth.cache_data_hash(prepared[tf]))

    def test_downstream_wrappers_do_not_change_outputs_and_nexus_scope_is_separate(self):
        scoring = FakeScoring(); nexus = FakeNexus()
        engine_cls = type("EngineCase", (FakeEngine,), {})
        client_cls = type("ClientCase", (FakeClient,), {})
        # Make EngineCase instantiate the class that hooks will wrap.
        def init(self):
            self.client = client_cls(); self.scoring = scoring; self.nexus = nexus
        engine_cls.__init__ = init
        baseline = asyncio.run(FakeEngine(FakeScoring(), FakeNexus())._open(FakeSignal()))
        hooks.install_engine_and_downstream(engine_cls, client_cls, scoring, nexus)
        engine = engine_cls()
        sig = FakeSignal()
        strategy_eval = truth.new_evaluation_id("POLICY_A_STRATEGY",sig.symbol)
        hooks._SIGNAL_EVALUATIONS[id(sig)] = (strategy_eval,"POLICY_A_STRATEGY")
        observed = asyncio.run(engine._open(sig))
        self.assertEqual(observed, baseline)
        events = truth.RECORDER.drain(1000,4_000_000)
        result_events = [e for e in events if e["event_type"]=="MARKET_ANALYSIS_RESULT"]
        stages = {e["payload"].get("stage") for e in result_events}
        self.assertTrue({"ENGINE_CANDIDATE_RESULT","NEXUS_RESULT","RISK_PRETRADE_RESULT","EXCHANGE_DISPATCH_RESULT","FILL_RESULT"}.issubset(stages))
        nexus_inputs = [e for e in events if e["event_type"]=="MARKET_ANALYSIS_INPUT" and e["payload"].get("evaluation_scope")=="NEXUS"]
        self.assertEqual(len(nexus_inputs),1)
        self.assertNotEqual(nexus_inputs[0]["payload"]["evaluation_id"], strategy_eval)


if __name__ == "__main__":
    unittest.main()
