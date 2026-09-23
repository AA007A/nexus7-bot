import ast
import inspect
import os
import unittest

from bot import runtime_truth as truth
from bot import runtime_truth_exporter


class RuntimeTruthSecurityTests(unittest.TestCase):
    def test_core_and_exporter_have_no_exchange_mutation_imports_or_calls(self):
        forbidden_imports = {
            "bot.kucoin", "bot.engine", "bot.execution_capability",
            "bot.native_stop_repair", "bot.durable_execution",
        }
        forbidden_calls = {
            "place_order", "_post", "cancel_all_orders", "set_sl",
            "set_position_stops", "set_position_stops", "submit_order",
        }
        for module in (truth, runtime_truth_exporter):
            source = inspect.getsource(module)
            tree = ast.parse(source)
            imported = set()
            called = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add(node.module or "")
                elif isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Attribute): called.add(fn.attr)
                    elif isinstance(fn, ast.Name): called.add(fn.id)
            self.assertFalse(imported & forbidden_imports, f"forbidden imports in {module.__name__}: {imported & forbidden_imports}")
            self.assertFalse(called & forbidden_calls, f"forbidden exchange calls in {module.__name__}: {called & forbidden_calls}")

    def test_truth_events_do_not_read_exchange_credentials(self):
        old_enabled, old_recorder = truth._ENABLED, truth.RECORDER
        try:
            truth._ENABLED = True
            truth.RECORDER = truth.Recorder(); truth.RECORDER.enabled = True
            secrets = {
                "KUCOIN_API_KEY":"DO_NOT_LEAK_KEY",
                "KUCOIN_API_SECRET":"DO_NOT_LEAK_SECRET",
                "KUCOIN_API_PASSPHRASE":"DO_NOT_LEAK_PASSPHRASE",
            }
            old = {k: os.environ.get(k) for k in secrets}
            os.environ.update(secrets)
            truth.capture_ws_application_payload('{"type":"message","topic":"/contractMarket/limitCandle:XBTUSDTM_15min","data":{"candles":["1","2","3","4","1","10","20"]}}', "session-safe")
            truth.capture_rest_response("/api/v1/kline/query", {"symbol":"XBTUSDTM","granularity":"15"}, 200, b'{"code":"200000","data":[]}')
            serialized = str(truth.RECORDER.drain(20, 1_000_000))
            for secret in secrets.values(): self.assertNotIn(secret, serialized)
            for k,v in old.items():
                if v is None: os.environ.pop(k,None)
                else: os.environ[k]=v
        finally:
            truth._ENABLED, truth.RECORDER = old_enabled, old_recorder

    def test_feature_disabled_does_not_import_exporter_from_core(self):
        core_source = inspect.getsource(truth)
        self.assertNotIn("runtime_truth_exporter", core_source)


if __name__ == "__main__":
    unittest.main()
