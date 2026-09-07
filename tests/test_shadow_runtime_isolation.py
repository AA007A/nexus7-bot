import logging
import os
from types import SimpleNamespace
from unittest.mock import patch


def _blocked_issue(code):
    return SimpleNamespace(code=code, severity=SimpleNamespace(value="BLOCKED"))


class _Guard:
    def __init__(self, codes):
        self.state = SimpleNamespace(
            checked_at=10**12,
            issues=[_blocked_issue(code) for code in codes],
        )

    def can_open_new(self):
        return False


def test_shadow_manual_position_only_blockers_allow_hypothetical_scan():
    from bot import shadow_integrity_isolation as iso

    guard = _Guard(["STATE_DIVERGENCE", "POSITION_WITHOUT_STOP"])
    engine = SimpleNamespace(
        integrity=guard,
        paper_trade=False,
        _validation_safety_lock_active=True,
    )
    log = SimpleNamespace(warning=lambda *a, **k: None)
    iso.install_for_engine(engine, log)
    assert guard.can_open_new() is True


def test_shadow_hard_integrity_fault_stays_fail_closed():
    from bot import shadow_integrity_isolation as iso

    guard = _Guard(["STATE_DIVERGENCE", "REST_UNAVAILABLE"])
    engine = SimpleNamespace(
        integrity=guard,
        paper_trade=False,
        _validation_safety_lock_active=True,
    )
    log = SimpleNamespace(warning=lambda *a, **k: None)
    iso.install_for_engine(engine, log)
    assert guard.can_open_new() is False


def test_isolation_does_not_apply_without_validation_lock():
    from bot import shadow_integrity_isolation as iso

    guard = _Guard(["STATE_DIVERGENCE"])
    engine = SimpleNamespace(
        integrity=guard,
        paper_trade=False,
        _validation_safety_lock_active=False,
    )
    log = SimpleNamespace(warning=lambda *a, **k: None)
    iso.install_for_engine(engine, log)
    assert guard.can_open_new() is False


def test_startup_banner_is_rewritten_for_nonpaper():
    from bot import shadow_startup_logging as mod

    old_factory = logging.getLogRecordFactory()
    old_installed = mod._INSTALLED
    old_original = mod._ORIGINAL_FACTORY
    try:
        with patch.dict(os.environ, {"PAPER_TRADE": "false"}, clear=False):
            mod._INSTALLED = False
            mod._ORIGINAL_FACTORY = None
            mod.install_preimport()
            record = logging.getLogRecordFactory()(
                "test", logging.CRITICAL, __file__, 1,
                "🔴 OPERAÇÃO REAL ATIVA — ordens serão enviadas à KuCoin",
                (), None,
            )
            assert "SHADOW LIVE ATIVO" in record.getMessage()
            assert "OPERAÇÃO REAL ATIVA" not in record.getMessage()
            assert record.levelno == logging.WARNING
    finally:
        logging.setLogRecordFactory(old_factory)
        mod._INSTALLED = old_installed
        mod._ORIGINAL_FACTORY = old_original


def test_sitecustomize_installs_banner_rewrite_before_engine_import():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "sitecustomize.py").read_text()
    install_idx = src.index("_shadow_startup_logging.install_preimport()")
    engine_idx = src.index("from bot.engine import TradingEngine")
    assert install_idx < engine_idx
