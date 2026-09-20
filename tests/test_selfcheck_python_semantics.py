import tempfile
from pathlib import Path
import unittest
from bot.selfcheck import check_duplicate_methods, check_undefined_names


class PythonSemanticsTests(unittest.TestCase):
    def inspect(self, source, checker):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'example.py'
            p.write_text(source)
            return checker([str(p)])

    def test_module_file_is_defined_but_misspelling_is_not(self):
        issues = self.inspect('def path():\n    return __file__, missing_file\n', check_undefined_names)
        self.assertEqual(len(issues), 1)
        self.assertIn('missing_file', issues[0])

    def test_property_setter_and_deleter_are_distinct_accessors(self):
        source = 'class C:\n    @property\n    def value(self): return 1\n    @value.setter\n    def value(self, x): pass\n    @value.deleter\n    def value(self): pass\n'
        self.assertEqual(self.inspect(source, check_duplicate_methods), [])

    def test_repeated_setter_is_still_duplicate(self):
        source = 'class C:\n    @property\n    def value(self): return 1\n    @value.setter\n    def value(self, x): pass\n    @value.setter\n    def value(self, x): pass\n'
        self.assertEqual(len(self.inspect(source, check_duplicate_methods)), 1)

    def test_ordinary_duplicate_and_unrelated_setter_are_not_exempt(self):
        for decorator in ('', '    @other.setter\n'):
            source = 'class C:\n    def value(self): return 1\n' + decorator + '    def value(self): return 2\n'
            self.assertEqual(len(self.inspect(source, check_duplicate_methods)), 1)

    def test_actual_repository_has_no_critical_integrity_findings(self):
        from bot.selfcheck import run_selfcheck
        self.assertEqual(run_selfcheck(verbose=False)['critical'], [])
