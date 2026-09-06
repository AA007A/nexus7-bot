from bot.startup_block import classify_startup_block, telegram_block_message


def test_healthy_startup_is_not_blocked():
    assert classify_startup_block(sitecustomize_status="ok", critical_issues=[]) is None


def test_warnings_are_not_part_of_block_contract():
    # The classifier accepts only structural critical findings; warnings cannot
    # accidentally become a startup blocker.
    assert classify_startup_block(sitecustomize_status="ok", critical_issues=()) is None


def test_sitecustomize_reason_is_exact():
    block = classify_startup_block(sitecustomize_status="not_loaded", critical_issues=[])
    assert block is not None
    assert block.code == "SITECUSTOMIZE_NOT_CONFIRMED"


def test_selfcheck_exception_reason_is_exact():
    block = classify_startup_block(
        sitecustomize_status="ok",
        critical_issues=[],
        selfcheck_error=RuntimeError("secret detail must not leak"),
    )
    assert block is not None
    assert block.code == "SELFCHECK_EXCEPTION"
    assert "secret detail" not in block.detail


def test_structural_critical_reason_is_exact():
    block = classify_startup_block(
        sitecustomize_status="ok",
        critical_issues=["NameError latent", "AttributeError latent"],
    )
    assert block is not None
    assert block.code == "SELFCHECK_CRITICAL"
    assert "2 structural" in block.detail


def test_telegram_message_contains_reason_and_startup_id():
    block = classify_startup_block(sitecustomize_status="bad", critical_issues=[])
    assert block is not None
    msg = telegram_block_message(block, "railway-deploy-123")
    assert "SITECUSTOMIZE_NOT_CONFIRMED" in msg
    assert "railway-deploy-123" in msg
    assert "bugs críticos" not in msg.lower()
