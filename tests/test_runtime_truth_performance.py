import json
import os
import statistics
import time
import tracemalloc
import unittest
from unittest.mock import patch

from bot import runtime_truth as truth


def percentile(values, p):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered)-1, max(0, int(round((len(ordered)-1)*p))))
    return ordered[index]


class RuntimeTruthPerformanceTests(unittest.TestCase):
    def test_measured_producer_performance(self):
        old_enabled, old_recorder = truth._ENABLED, truth.RECORDER
        try:
            truth._ENABLED = True
            with patch.object(truth, "_MAX_EVENTS", 8192), patch.object(truth, "_MAX_BYTES", 32*1024*1024), patch.object(truth, "_MAX_EVENT_BYTES", 262144):
                recorder = truth.Recorder()
            truth.RECORDER = recorder
            tracemalloc.start()
            cpu0 = time.process_time_ns()
            wall0 = time.perf_counter_ns()
            for i in range(5000):
                recorder.emit("MARKET_TEST", symbol="LTCUSDT", timeframe="15", payload={"i":i,"v":123.456,"text":"x"*64})
                if i and i % 1000 == 0:
                    recorder.drain(900, 8*1024*1024)
            wall1 = time.perf_counter_ns(); cpu1 = time.process_time_ns()
            current_mem, peak_mem = tracemalloc.get_traced_memory(); tracemalloc.stop()
            metrics = recorder.metrics()
            enqueue_us = [x/1000.0 for x in metrics["enqueue_ns"]]
            serialization_us = [x/1000.0 for x in metrics["serialization_ns"]]

            rows = [{"ts":1790000000000+i*900000,"o":100.0+i,"h":101.0+i,"l":99.0+i,"c":100.5+i,"v":1000.0+i} for i in range(100)]
            analysis_samples = []
            eval_id = truth.new_evaluation_id("POLICY_A_STRATEGY","LTCUSDT")
            with truth.evaluation_context(eval_id,"POLICY_A_STRATEGY",{"_raw_inputs":{"15":rows,"60":rows,"240":rows}}):
                for _ in range(200):
                    t0=time.perf_counter_ns()
                    truth.capture_analysis_input("LTCUSDT",{"15":rows,"60":rows,"240":rows},{"15":rows,"60":rows,"240":rows},1790100000000)
                    analysis_samples.append((time.perf_counter_ns()-t0)/1_000_000.0)
                    recorder.drain(1, 2*1024*1024)

            result = {
                "enqueue_us":{"p50":round(percentile(enqueue_us,.50),3),"p95":round(percentile(enqueue_us,.95),3),"p99":round(percentile(enqueue_us,.99),3)},
                "serialization_us":{"p50":round(percentile(serialization_us,.50),3),"p95":round(percentile(serialization_us,.95),3),"p99":round(percentile(serialization_us,.99),3)},
                "analysis_capture_ms":{"p50":round(percentile(analysis_samples,.50),4),"p95":round(percentile(analysis_samples,.95),4),"p99":round(percentile(analysis_samples,.99),4)},
                "cpu_ms":round((cpu1-cpu0)/1_000_000.0,3),
                "wall_ms":round((wall1-wall0)/1_000_000.0,3),
                "memory_current_bytes":current_mem,
                "memory_peak_bytes":peak_mem,
                "queue_depth":metrics["queue_depth"],
                "queue_bytes":metrics["queue_bytes"],
                "dropped":recorder.reported_dropped_total(),
            }
            print("PERFORMANCE_RESULTS="+json.dumps(result,sort_keys=True))
            self.assertLessEqual(metrics["queue_bytes"], recorder.max_bytes)
            self.assertEqual(recorder.reported_dropped_total(), 0)
            self.assertGreater(len(enqueue_us), 0)
            self.assertGreater(len(serialization_us), 0)
        finally:
            truth._ENABLED, truth.RECORDER = old_enabled, old_recorder

    def test_saturation_memory_remains_bounded(self):
        old_enabled, old_recorder = truth._ENABLED, truth.RECORDER
        try:
            truth._ENABLED = True
            with patch.object(truth,"_MAX_EVENTS",32), patch.object(truth,"_MAX_BYTES",32768), patch.object(truth,"_MAX_EVENT_BYTES",4096):
                recorder=truth.Recorder()
            truth.RECORDER=recorder
            for i in range(10000): recorder.emit("MARKET_TEST",payload={"i":i,"blob":"x"*1024})
            metrics=recorder.metrics()
            self.assertLessEqual(metrics["queue_depth"],32)
            self.assertLessEqual(metrics["queue_bytes"],32768)
            self.assertGreater(recorder.telemetry_dropped_total,0)
        finally:
            truth._ENABLED, truth.RECORDER = old_enabled, old_recorder


if __name__ == "__main__":
    unittest.main()
