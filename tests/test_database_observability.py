import pytest

from bot import database


class FailingPG:
    async def fetch(self, *args, **kwargs):
        raise RuntimeError("read boom")

    async def fetchrow(self, *args, **kwargs):
        raise RuntimeError("read boom")


@pytest.mark.asyncio
async def test_fetchall_non_strict_is_observable_and_returns_empty(monkeypatch, caplog):
    monkeypatch.setattr(database, "_conn", FailingPG())
    monkeypatch.setattr(database, "_is_pg", True)
    rows = await database._fetchall("SELECT 1")
    assert rows == []
    assert "DB fetchall failed" in caplog.text


@pytest.mark.asyncio
async def test_fetchall_strict_fails_closed(monkeypatch):
    monkeypatch.setattr(database, "_conn", FailingPG())
    monkeypatch.setattr(database, "_is_pg", True)
    with pytest.raises(database.PersistenceError):
        await database._fetchall("SELECT 1", strict=True)


@pytest.mark.asyncio
async def test_fetchone_non_strict_is_observable(monkeypatch, caplog):
    monkeypatch.setattr(database, "_conn", FailingPG())
    monkeypatch.setattr(database, "_is_pg", True)
    row = await database._fetchone("SELECT 1")
    assert row is None
    assert "DB fetchone failed" in caplog.text
