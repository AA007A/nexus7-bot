"""Offline regressions for audit fixes; no exchange mutation or network."""
import builtins
import sys
import subprocess
import unittest
from unittest.mock import patch
from bot import nexus_ai
from bot.nexus_types import Decision, PositionAction, Regime
from bot.nexus_probability import heuristic_win_probability
from bot.oos_calibration_readiness import normalize_completed_rows, build_readiness, compact_readiness_log
from bot.startup_block import StartupBlock, telegram_block_message


class AuditRemainingTests(unittest.TestCase):
    def test_ev_transform_matches_legacy_for_valid_scores(self):
        for confidence in (0, 7, 50, 80, 99.99, 100):
            self.assertEqual(heuristic_win_probability(confidence),
                             min(0.75, 0.30 + confidence / 100 * 0.45))

    def test_invalid_confidence_is_excluded_from_oos(self):
        values = (-1, 101, float("nan"), float("inf"), True, None, "bad")
        rows, invalid = normalize_completed_rows([(100, v, 1, 1.0) for v in values])
        self.assertEqual(rows, [])
        self.assertEqual(invalid, len(values))

    def test_report_identifies_reconstructed_uncalibrated_probability(self):
        text = compact_readiness_log(build_readiness([]))
        self.assertIn("probability_model=ensemble_linear_v1", text)
        self.assertIn("empirically_calibrated=False", text)

    def test_trailing_at_two_r_and_break_even_at_one_r_long_and_short(self):
        candles = [{"c": 100, "h": 101, "l": 99, "v": 10} for _ in range(30)]
        for side, stop, target, sign in (("LONG", 90, 140, 1), ("SHORT", 110, 60, -1)):
            direction = Decision.LONG if side == "LONG" else Decision.SHORT
            with patch.object(nexus_ai, "detect_regime", return_value=(Regime.RANGE, {})), \
                 patch.object(nexus_ai, "run_ensemble", return_value=[]), \
                 patch.object(nexus_ai, "_fuse", return_value={"direction": direction, "confidence": 50}):
                for r, action in ((0.5, PositionAction.HOLD), (1, PositionAction.MOVE_STOP),
                                  (1.99, PositionAction.MOVE_STOP), (2, PositionAction.TRAIL_STOP),
                                  (3, PositionAction.TRAIL_STOP)):
                    with self.subTest(side=side, r=r):
                        result = nexus_ai.monitor_position("BTCUSDT", side, 100, stop, target,
                                                           100 + sign * r * 10, candles)
                        self.assertEqual(result["action"], action.value)

    def test_not_loaded_startup_detail_has_escaped_markdown(self):
        msg = telegram_block_message(StartupBlock("SITECUSTOMIZE_NOT_CONFIRMED",
                                                  "sitecustomize=not_loaded"))
        self.assertIn(r"sitecustomize=not\_loaded", msg)

    def test_console_startup_without_automatic_sitecustomize(self):
        code = (
            "from tests.run_offline import install_network_guard; install_network_guard(); "
            "import builtins; "
            "assert not hasattr(builtins, '_nexus_sitecustomize_status'); "
            "import main_hardened; "
            "assert builtins._nexus_sitecustomize_status == 'ok'; "
            "assert builtins._nexus_runtime_bootstrap_installed"
        )
        with patch.dict("os.environ", {"PAPER_TRADE": "true",
                                       "MIN_ENTRY_SCORE": "60", "NEXUS_MIN_SCORE": "60"}):
            result = subprocess.run([sys.executable, "-S", "-c", code],
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_hardened_entrypoint_explicitly_imports_bootstrap(self):
        # Exercise the console-script case without running the real bootstrap:
        # importing sitecustomize must occur before main, even with no marker.
        import runpy
        import types
        from pathlib import Path
        seen = []
        original_import = builtins.__import__
        sentinel = RuntimeError("stop before application composition")

        def importer(name, *args, **kwargs):
            if name == "sitecustomize":
                seen.append(name)
                return types.ModuleType(name)
            if name == "main":
                seen.append(name)
                raise sentinel
            return original_import(name, *args, **kwargs)

        with patch.dict("os.environ", {"MIN_ENTRY_SCORE": "60", "NEXUS_MIN_SCORE": "60"}), \
             patch("builtins.__import__", side_effect=importer):
            with self.assertRaisesRegex(RuntimeError, "stop before application composition"):
                runpy.run_path(str(Path(__file__).resolve().parents[1] / "main_hardened.py"))
        self.assertEqual(seen, ["sitecustomize", "main"])


if __name__ == "__main__":
    unittest.main()
