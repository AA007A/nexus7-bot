from bot.balance_observability import exchange_metadata, paper_metadata


def test_exchange_balance_is_read_only_in_paper():
    meta = exchange_metadata(0.49173, True)
    assert meta == {
        "available_usdt": 0.4917,
        "source": "KuCoin",
        "read_only": True,
        "mode": "PAPER_READ_ONLY",
        "used_for_paper_sizing": False,
    }


def test_exchange_balance_is_read_only_in_shadow():
    meta = exchange_metadata(12.34567, False, True)
    assert meta["available_usdt"] == 12.3457
    assert meta["read_only"] is True
    assert meta["mode"] == "SHADOW_READ_ONLY"
    assert meta["used_for_paper_sizing"] is False


def test_exchange_balance_is_live_only_when_execution_mode_is_live():
    meta = exchange_metadata(12.34567, False, False)
    assert meta["available_usdt"] == 12.3457
    assert meta["read_only"] is False
    assert meta["mode"] == "LIVE"
    assert meta["used_for_paper_sizing"] is False


def test_paper_wallet_is_explicit_sizing_source():
    meta = paper_metadata(20.0)
    assert meta == {
        "balance": 20.0,
        "source": "isolated_paper_wallet",
        "used_for_paper_sizing": True,
    }
