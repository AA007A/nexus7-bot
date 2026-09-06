"""Startup must block if the integrity self-check itself cannot complete."""
import ast
from pathlib import Path
import unittest


class StartupSelfcheckFailClosedTests(unittest.TestCase):
    def test_selfcheck_exception_flows_into_fail_closed_classifier(self):
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8"))
        guarded = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            imports_selfcheck = any(
                isinstance(child, ast.ImportFrom)
                and child.module == "bot.selfcheck"
                for child in ast.walk(ast.Module(body=node.body, type_ignores=[]))
            )
            if imports_selfcheck:
                guarded.append(node)

        self.assertEqual(len(guarded), 1)
        error_assignments = [
            child for handler in guarded[0].handlers for child in ast.walk(handler)
            if isinstance(child, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id == "_selfcheck_error"
                    for target in child.targets)
        ]
        self.assertEqual(len(error_assignments), 1)
        self.assertIsInstance(error_assignments[0].value, ast.Name)

        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "classify_startup_block"
        ]
        self.assertEqual(len(calls), 1)
        keyword_names = {kw.arg for kw in calls[0].keywords}
        self.assertIn("selfcheck_error", keyword_names)

        blocked_assignments = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id == "_blocked_by_selfcheck"
                    for target in node.targets)
        ]
        self.assertEqual(len(blocked_assignments), 1)
        self.assertIsInstance(blocked_assignments[0].value, ast.Compare)


if __name__ == "__main__":
    unittest.main()
