"""Regression tests for the stop-beyond-liquidation escape hatch."""
from __future__ import annotations

import os
import subprocess
import sys


def _run(code: str, **env_updates):
    env = os.environ.copy()
    env.update({k: str(v) for k, v in env_updates.items()})
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_guard_accepts_default_safe_state():
    code = (
        "import os; "
        "os.environ.pop('ALLOW_SL_BEYOND_LIQUIDATION', None); "
        "from bot.liquidation_override_guard import install; "
        "install(type('L', (), {'info': staticmethod(lambda *a, **k: None)})()); "
        "print('ok')"
    )
    result = _run(code, PAPER_TRADE="true")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_guard_rejects_explicit_unsafe_override():
    code = (
        "from bot.liquidation_override_guard import install; "
        "install(type('L', (), {'info': staticmethod(lambda *a, **k: None)})())"
    )
    result = _run(code, ALLOW_SL_BEYOND_LIQUIDATION="true", PAPER_TRADE="true")
    assert result.returncode != 0
    assert "unsafe liquidation override is forbidden" in result.stderr


def test_sitecustomize_fails_closed_when_override_is_true():
    code = (
        "import builtins; "
        "print(getattr(builtins, '_nexus_sitecustomize_status', 'missing'))"
    )
    result = _run(code, ALLOW_SL_BEYOND_LIQUIDATION="true", PAPER_TRADE="true")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("failed")
    assert "CRITICAL: NEXUS sitecustomize hardening installation failed" in result.stderr
