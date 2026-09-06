from pathlib import Path


def test_main_integrates_startup_block_classifier():
    text = Path("main.py").read_text(encoding="utf-8")
    assert "from bot.startup_block import classify_startup_block, telegram_block_message" in text
    assert "_startup_block = classify_startup_block(" in text
    assert "critical_issues=(_report or {}).get(\"critical\", ())" in text
    assert "selfcheck_error=_selfcheck_error" in text


def test_blocked_startup_uses_exact_reason_message():
    text = Path("main.py").read_text(encoding="utf-8")
    assert "await _n(telegram_block_message(_startup_block, _startup_id))" in text
    assert "Engine NÃO iniciado — code=%s startup_id=%s" in text
    assert "O self-check detectou bug(s) crítico(s) no código." not in text


def test_startup_block_is_observable_without_changing_trading_mode():
    text = Path("main.py").read_text(encoding="utf-8")
    assert '"startup_block": getattr(app.state, "startup_block", None)' in text
    assert '"trading_mode": "PAPER" if PAPER_TRADE else "LIVE"' in text
    assert '"orders_sent_to_exchange": not PAPER_TRADE' in text
