import pytest

from bot import database


class FailingRollbackConn:
    async def execute(self, *args, **kwargs):
        raise RuntimeError("write boom")

    async def rollback(self):
        raise RuntimeError("rollback boom")


@pytest.mark.asyncio
async def test_trade_open_rollback_failure_does_not_mask_primary_error(monkeypatch, caplog):
    monkeypatch.setattr(database, "_conn", FailingRollbackConn())
    monkeypatch.setattr(database, "_is_pg", False)
    with pytest.raises(database.PersistenceError) as excinfo:
        await database.save_trade_open("BTCUSDT", "LONG", 100.0, 1.0, 2, 60)
    assert "trade open insert failed" in str(excinfo.value)
    assert "rollback failed" in caplog.text
