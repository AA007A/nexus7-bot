import ast
from pathlib import Path


def _silent_except_lines(source: str):
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            out.append(node.lineno)
    return out


def test_runtime_bootstrap_has_no_silent_except_pass():
    root = Path(__file__).resolve().parents[1]
    source = (root / "bot" / "runtime_bootstrap.py").read_text(encoding="utf-8")
    assert _silent_except_lines(source) == []


def test_best_effort_observability_failures_are_explicitly_logged():
    root = Path(__file__).resolve().parents[1]
    source = (root / "bot" / "runtime_bootstrap.py").read_text(encoding="utf-8")
    for marker in (
        "[MTF_SHADOW] observability_failed",
        "[NEXUS_PERSISTENCE] best_effort_failed",
        "[NEXUS_NOTIFY] best_effort_schedule_failed",
        "[STATUS_OBSERVABILITY] best_effort_failed",
    ):
        assert marker in source
