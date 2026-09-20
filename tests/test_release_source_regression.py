"""Baseline identity proof for alpha and sizing modules.

The expected hashes are Git blob IDs from production baseline
357f1e6fa79c7be4ddf0bf175e426573037e88e9.  Infrastructure remediation
must not alter these modules; behavioral regression is therefore stronger than
a sampled snapshot for these pure authorities.
"""
import hashlib
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASELINE={
 "bot/nexus_ai.py":"ef1a8e49614d4a343762cc6f07476c522928dd1b",
 "bot/nexus_models.py":"c0eac180d195408543f8997e0f575291c8abb6c5",
 "bot/nexus_probability.py":"325d614cd89ce111a95db2d44497a3c779f38d7c",
 "bot/indicators.py":"268fadc56d6a261d836144545138c7140bc51345",
 "bot/risk.py":"e6bdb2e2719e281449e49add189233f5c928aff6",
 "bot/quantity.py":"b081a785e67e7f33229c40dd0bb21061600eed96",
}
def git_blob_sha(data:bytes)->str:
    return hashlib.sha1(b"blob "+str(len(data)).encode()+b"\0"+data).hexdigest()

class ReleaseSourceRegression(unittest.TestCase):
    def test_strategy_and_sizing_authorities_are_byte_identical_to_production_baseline(self):
        for path,expected in BASELINE.items():
            with self.subTest(path=path):
                self.assertEqual(git_blob_sha((ROOT/path).read_bytes()),expected)

if __name__=="__main__":
    unittest.main()
