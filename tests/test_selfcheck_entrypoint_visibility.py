import os
import unittest

from bot import selfcheck


class SelfcheckEntrypointVisibilityTests(unittest.TestCase):
    def test_service_readiness_is_not_reported_as_orphan(self):
        warnings = selfcheck.check_orphan_modules(selfcheck._python_files())
        offenders = [w for w in warnings if "service_readiness.py" in w]
        self.assertEqual(offenders, [])

    def test_sitecustomize_documents_main_hardened_readiness_dependency(self):
        path = os.path.join(selfcheck._ROOT, "sitecustomize.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("bot.service_readiness", src)
        self.assertIn("main_hardened.py", src)


if __name__ == "__main__":
    unittest.main()
