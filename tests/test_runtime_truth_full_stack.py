import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


SCRIPT = r'''
import json
from unittest.mock import patch
from bot.strategy import Analyzer

def rows(count, width_ms, slope):
    end = 1790179200000 - width_ms
    start = end - (count-1)*width_ms
    out=[]
    for i in range(count):
        c=100.0 + slope*i
        o=c - slope*0.35
        out.append({"ts":start+i*width_ms,"o":o,"h":max(o,c)+0.7,"l":min(o,c)-0.7,"c":c,"v":1000.0+(i%17)*37.0})
    return out

k15=rows(220,900000,-0.08)
k1=rows(220,3600000,-0.12)
k4=rows(220,14400000,-0.18)
with patch("time.time", return_value=1790179201.0):
    result=Analyzer().analyze_mtf("LTCUSDT",k15,k1,k4,min_score=60,fee_mult=2.0,vol_mult=1.0)
if result is None:
    payload=None
else:
    keys=("symbol","direction","entry","sl","tp","confidence","reason","score","tf_4h","tf_1h","tf_15m","expected_pnl","total_fees","entry_type","regime","tp1","tp2","rr1","rr2","rr")
    payload={k:getattr(result,k,None) for k in keys}
print("RESULT_JSON="+json.dumps(payload,sort_keys=True,default=str))
'''


class FullStackTruthParityTests(unittest.TestCase):
    def _run(self, enabled):
        env=os.environ.copy()
        env["BGX_RUNTIME_TRUTH_ENABLED"]="true" if enabled else "false"
        env.pop("BGX_TRUTH_SINK_URL",None)
        env.pop("BGX_TRUTH_INGEST_TOKEN",None)
        proc=subprocess.run([sys.executable,"-c",SCRIPT],cwd=str(Path(__file__).resolve().parents[1]),env=env,text=True,capture_output=True,timeout=60)
        self.assertEqual(proc.returncode,0,proc.stdout+"\n"+proc.stderr)
        lines=[line for line in proc.stdout.splitlines() if line.startswith("RESULT_JSON=")]
        self.assertTrue(lines,proc.stdout+"\n"+proc.stderr)
        return json.loads(lines[-1].split("=",1)[1])

    def test_full_strategy_stack_output_identical_off_vs_on(self):
        off=self._run(False)
        on=self._run(True)
        self.assertEqual(on,off)


if __name__ == "__main__":
    unittest.main()
