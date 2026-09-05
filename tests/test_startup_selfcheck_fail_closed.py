"""Startup must block if the integrity self-check itself cannot complete."""
import ast
from pathlib import Path
import unittest


class StartupSelfcheckFailClosedTests(unittest.TestCase):
    def test_selfcheck_exception_sets_block_flag(self):
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
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
        assignments = [
            child for handler in guarded[0].handlers for child in ast.walk(handler)
            if isinstance(child, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id == "_blocked_by_selfcheck"
                    for target in child.targets)
        ]
        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, ast.Constant)
        self.assertIs(assignments[0].value.value, True)


if __name__ == "__main__":
    unittest.main()
