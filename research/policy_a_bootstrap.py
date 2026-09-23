import runpy
import sitecustomize  # frozen production bootstrap from the reference cohort

runpy.run_path("research/policy_a_parity.py", run_name="__main__")
