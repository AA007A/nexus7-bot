import pytest

from bot import kucoin_order_forensics as forensic


class FakeSession:
    closed = False

    async def close(self):
        self.closed = True


class FakeClient:
    def __init__(self):
        self._session = FakeSession()
        self.get_calls = []
        self.raw_calls = []

    async def get_order_status(self, order_id):
        self.get_calls.append(order_id)
        return {
            "id": order_id,
            "clientOid": "bgx7-test",
            "symbol": "ATOMUSDTM",
            "type": "market",
            "stopTriggered": True,
            "stopPrice": "1.7554",
            "avgDealPrice": "1.749812707",
            "filledSize": "2109",
            "reduceOnly": True,
            "closeOrder": False,
            "secret": "must-not-leak",
        }

    async def _get(self, endpoint, params=None, auth=False):
        self.raw_calls.append((endpoint, params, auth))
        return {
            "items": [
                {
                    "tradeId": "t1",
                    "orderId": params["orderId"],
                    "price": "1.7498",
                    "size": "2109",
                    "fee": "0.4447",
                    "private": "must-not-leak",
                },
                {"tradeId": "other", "orderId": "999", "price": "1"},
            ]
        }


@pytest.mark.asyncio
async def test_snapshot_is_read_only_and_redacted(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(forensic, "KuCoinClient", lambda: fake)

    result = await forensic.snapshot_order("491192937082871808")

    assert result["forensic_mode"] == "READ_ONLY"
    assert result["execution_effect"] == "NONE"
    assert result["order"]["stopTriggered"] is True
    assert result["order"]["avgDealPrice"] == "1.749812707"
    assert "secret" not in result["order"]
    assert result["fills_count"] == 1
    assert "private" not in result["fills"][0]
    assert fake.get_calls == ["491192937082871808"]
    assert fake.raw_calls == [
        ("/api/v1/fills", {"orderId": "491192937082871808"}, True)
    ]
    assert fake._session.closed is True


@pytest.mark.asyncio
async def test_rejects_non_numeric_order_id():
    with pytest.raises(ValueError):
        await forensic.snapshot_order("not-an-order")
