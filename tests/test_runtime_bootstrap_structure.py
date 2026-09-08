from pathlib import Path


def test_sitecustomize_is_thin_fail_closed_entrypoint():
    text = Path("sitecustomize.py").read_text(encoding="utf-8")

    assert "from bot.runtime_bootstrap import install" in text
    assert '_nexus_sitecustomize_status = "installing"' in text
    assert '_nexus_sitecustomize_status = "ok"' in text
    assert '_nexus_sitecustomize_status = "failed"' in text
    assert "CRITICAL: NEXUS sitecustomize hardening installation failed" in text

    # Installation details and monkey-patch compatibility code no longer belong
    # to the implicit Python startup hook.
    assert "TradingEngine._nexus_validate =" not in text
    assert "Analyzer.analyze_mtf =" not in text
    assert "_np._execute =" not in text
    assert "validation_safety_lock" not in text


def test_runtime_bootstrap_keeps_validation_lock_in_install_order():
    text = Path("bot/runtime_bootstrap.py").read_text(encoding="utf-8")

    assert "_validation_safety_lock.install(_log)" in text
    assert "_prelive_protection_failclosed.install" in text
    assert "_pilot_external_position_guard.install" in text
    assert "_nexus_runtime_bootstrap_installed" in text
    assert "[RUNTIME_BOOTSTRAP]" in text
    assert "execution_effect=NONE" in text
