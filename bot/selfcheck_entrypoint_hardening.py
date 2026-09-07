"""Self-check entrypoint awareness for PRE-LIVE validation.

Modules intentionally executable with `python -m bot.<module>` are valid
entrypoints even when no project module imports them. Railway executes
`python -m bot.ci_deploy_gate` as its pre-deploy command, so a generic orphan
check must recognize modules with a __main__ guard.
"""
import ast
import os


def _has_main_guard(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except Exception:
        return False
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
            continue
        left, right = test.left, test.comparators[0]
        if (
            isinstance(test.ops[0], ast.Eq)
            and isinstance(left, ast.Name)
            and left.id == "__name__"
            and isinstance(right, ast.Constant)
            and right.value == "__main__"
        ):
            return True
    return False


def install(selfcheck_module, log):
    if getattr(selfcheck_module, "_entrypoint_orphan_hardened", False):
        return

    original = selfcheck_module.check_orphan_modules

    def _check_orphan_modules(paths):
        issues = original(paths)
        entrypoint_files = {
            os.path.basename(path)
            for path in paths
            if path.endswith(".py") and _has_main_guard(path)
        }
        return [
            issue
            for issue in issues
            if not any(issue.startswith(name + " ") for name in entrypoint_files)
        ]

    selfcheck_module.check_orphan_modules = _check_orphan_modules
    selfcheck_module._entrypoint_orphan_hardened = True
    log.info("[SELFCHECK] python -m entrypoints excluded from orphan-module warnings")
