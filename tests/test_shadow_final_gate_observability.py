import inspect
import unittest

from bot import shadow_live


class ShadowFinalGateObservabilityTests(unittest.TestCase):
    def test_observability_does_not_add_exchange_mutations(self):
        source = inspect.getsource(shadow_live)
        forbidden = (
            ".place_order(",
            ".cancel_order(",
            ".cancel_all_orders(",
            ".close_position(",
            ".set_leverage(",
            ".set_sl(",
            ".set_position_stops(",
        )
        for token in forbidden:
            self.assertNotIn(token, source, token)

    def test_final_gate_chain_is_explicit(self):
        source = inspect.getsource(shadow_live.evaluate_candidate)
        stages = (
            '"NEXUS_AI"',
            '"CAPITAL_HEALTH"',
            '"SIZING"',
            '"COLLATERAL"',
            '"PRETRADE_DATA"',
            '"PRETRADE_SCORE"',
            '"QUANTITY_VALIDATION"',
            '"PROTECTIVE_LEVELS"',
            '"LIQUIDATION_GUARD"',
            '"PILOT_OBSERVABILITY"',
        )
        for stage in stages:
            self.assertIn(stage, source)
        self.assertIn("WOULD_SUBMIT", source)
        self.assertIn('"execution_effect":"NONE"', source)

    def test_drawdown_gate_precedes_sizing(self):
        source = inspect.getsource(shadow_live.evaluate_candidate)
        drawdown = source.index("engine.risk.drawdown >= cfg.MAX_DRAWDOWN")
        sizing = source.index("engine.risk.size(")
        collateral = source.index("balance_semantics.collateral_allows(")
        would_submit = source.index("WOULD_SUBMIT")
        self.assertLess(drawdown, sizing)
        self.assertLess(sizing, collateral)
        self.assertLess(collateral, would_submit)

    def test_gate_logs_are_read_only_marked(self):
        source = inspect.getsource(shadow_live)
        self.assertIn("[SHADOW_GATE]", source)
        self.assertIn("execution_effect=NONE", source)


if __name__ == "__main__":
    unittest.main()
