import unittest

from bot import entry_type_shadow


class EntryTypeShadowTests(unittest.TestCase):
    def test_reclassifies_coherent_multibar_long_expansion(self):
        closes = [100.0, 100.1, 100.3, 100.7]
        opens = [99.9, 100.0, 100.15, 100.45]
        highs = [100.1, 100.2, 100.4, 100.75]
        lows = [99.8, 99.95, 100.1, 100.4]
        result = entry_type_shadow.assess(
            closes=closes, highs=highs, lows=lows, opens=opens,
            direction="LONG", atr_v=1.0, current_type="PULLBACK",
        )
        self.assertTrue(result["would_reclassify"])
        self.assertEqual(result["shadow_type"], "MOMENTUM_MULTI_BAR")

    def test_keeps_pullback_without_directional_persistence(self):
        closes = [100.0, 100.2, 100.0, 100.3]
        opens = [99.9, 100.3, 100.1, 100.2]
        highs = [100.1, 100.35, 100.15, 100.35]
        lows = [99.8, 100.1, 99.95, 100.15]
        result = entry_type_shadow.assess(
            closes=closes, highs=highs, lows=lows, opens=opens,
            direction="LONG", atr_v=1.0, current_type="PULLBACK",
        )
        self.assertFalse(result["would_reclassify"])
        self.assertIn("DIRECTIONAL_PERSISTENCE", result["failed_reasons"])

    def test_short_expansion_uses_direction_normalized_displacement(self):
        closes = [100.8, 100.6, 100.3, 99.9]
        opens = [100.9, 100.75, 100.5, 100.15]
        highs = [101.0, 100.8, 100.55, 100.2]
        lows = [100.7, 100.5, 100.2, 99.85]
        result = entry_type_shadow.assess(
            closes=closes, highs=highs, lows=lows, opens=opens,
            direction="SHORT", atr_v=1.0, current_type="PULLBACK",
        )
        self.assertTrue(result["would_reclassify"])
        self.assertGreaterEqual(result["net_3bar_atr"], 0.45)

    def test_non_pullback_is_not_eligible(self):
        result = entry_type_shadow.assess(
            closes=[1, 2, 3, 4], highs=[2, 3, 4, 5], lows=[0, 1, 2, 3],
            opens=[1, 2, 3, 4], direction="LONG", atr_v=1,
            current_type="MOMENTUM",
        )
        self.assertFalse(result["eligible"])
        self.assertFalse(result["would_reclassify"])


if __name__ == "__main__":
    unittest.main()
