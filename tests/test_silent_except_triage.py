import ast
import unittest
from pathlib import Path

from bot import silent_except_audit as audit


class SilentExceptTriageTests(unittest.TestCase):
    def _pass_handlers(self, filename):
        path = Path(audit._BOT_ROOT) / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return path, [
            node for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler)
            and node.body
            and all(isinstance(stmt, ast.Pass) for stmt in node.body)
        ]

    def test_engine_notification_only_handlers_are_best_effort(self):
        path, handlers = self._pass_handlers("engine.py")
        matched = []
        for node in handlers:
            text = audit._context_text(str(path), node)
            if "create_task(notify(" in text:
                matched.append(node)
                self.assertEqual(audit._classify(str(path), node), "BEST_EFFORT_LIKELY")
                reason = audit._justification(str(path), node, "BEST_EFFORT_LIKELY")
                self.assertIn("notification", reason.lower())
        self.assertGreaterEqual(len(matched), 2)

    def test_skip_source_is_explicitly_justified(self):
        path, handlers = self._pass_handlers("score.py")
        skip_handlers = []
        for node in handlers:
            exc_type = ast.unparse(node.type) if node.type is not None else ""
            if exc_type == "_SkipSource":
                skip_handlers.append(node)
                self.assertEqual(audit._classify(str(path), node), "BEST_EFFORT_LIKELY")
                reason = audit._justification(str(path), node, "BEST_EFFORT_LIKELY")
                self.assertIn("control-flow", reason)
        self.assertEqual(len(skip_handlers), 1)


if __name__ == "__main__":
    unittest.main()
