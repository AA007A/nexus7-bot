import runpy
import sys
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[1])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import sitecustomize  # frozen production bootstrap from the reference cohort

runpy.run_path(str(Path(repo_root) / "research" / "policy_a_parity.py"), run_name="__main__")
