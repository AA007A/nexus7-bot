import asyncio

from bot import kucoin_contract_risk_hardening as hardening
from bot.config import cfg
from bot.kucoin_contract_risk_hardening import (
    _contract_list,
    _normalize_mmr,
    _positive_finite,
    _select_cross_risk,
)


def test_normalize_mmr_accepts_fractional_exchange_value():
    assert _normalize_mmr(0.004) == 0.004
    assert _normalize_mmr("0.015") == 0.015


def test_normalize_mmr_rejects_missing_nonfinite_boolean_and_out_of_range():
    assert _normalize_mmr(None) is None
    assert _normalize_mmr(True) is None
    assert _normalize_mmr("nan") is None
    assert _normalize_mmr(0) is None
    assert _normalize_mmr(1) is None
    assert _normalize_mmr(-0.01) is None


def test_positive_finite_rejects_invalid_account_margin():
    assert _positive_finite("18.5482") == 18.5482
    assert _positive_finite(0) is None
    assert _positive_finite(-1) is None
    assert _positive_finite(False) is None
    assert _positive_finite("inf") is None


def test_contract_list_supports_kucoin_shapes_without_inventing_data():
    rows = [{"symbol": "ADAUSDTM", "maintainMargin": 0.015}]
    assert _contract_list(rows) is rows
    assert _contract_list({"data": rows}) is rows
    assert _contract_list({"dataList": rows}) is rows
    assert _contract_list({"items": rows}) is rows
    assert _contract_list({"data": {"symbol": "ADAUSDTM"}}) == []
    assert _contract_list(None) == []


def test_select_cross_risk_requires_exact_symbol_and_valid_mmr():
    rows = [
        {"symbol": "XBTUSDTM", "mmr": "0.0041", "leverage": "50"},
        {
            "symbol": "ADAUSDTM",
            "mmr": "0.0123",
            "imr": "0.0200",
            "totalMargin": "18.5482",
            "price": "0.205",
            "leverage": "50.00",
        },
    ]
    risk = _select_cross_risk(rows, "ADAUSDTM")
    assert risk["mmr"] == 0.0123
    assert risk["imr"] == 0.02
    assert risk["totalMargin"] == 18.5482
    assert risk["leverage"] == 50.0
    assert _select_cross_risk(rows, "ETHUSDTM") is None


def test_select_cross_risk_fails_closed_on_invalid_exact_row():
    rows = [
        {"symbol": "ADAUSDTM", "mmr": "nan", "leverage": "50"},
        {"symbol": "XBTUSDTM", "mmr": "0.004", "leverage": "50"},
    ]
    assert _select_cross_risk({"data": rows}, "ADAUSDTM") is None


class _Log:
    def warning(self, *args, **kwargs):
        pass


class _Liquidation:
    def __init__(self):
        self.values = {}

    def set_mmr_from_api(self, symbol, mmr, source="api"):
        self.values[symbol] = (float(mmr), source)

    def get_mmr(self, symbol):
        if symbol not in self.values:
            return 0.004, False
        return self.values[symbol][0], True


class _Pilot:
    enabled = True


class _Signal:
    symbol = "ADAUSDT"
    direction = "SHORT"


class _Scoring:
    events = None

    @staticmethod
    async def calculate(symbol, direction, closes, highs, lows, volumes, client=None):
        _Scoring.events.append("legacy_score")
        return {
            "total": 54,
            "tecnico": 20,
            "orderflow": 23,
            "macro": 11,
            "news_mod": 0,
            "aprovado": False,
        }


class _Client:
    _official_contract_mmr_installed = False

    def __init__(self, events, fail_cross=False):
        self.events = events
        self.fail_cross = fail_cross
        self._cross_mmr_symbols = set()

    async def load_instruments(self):
        return {"ADAUSDT": {"kucoinSymbol": "ADAUSDTM"}}

    async def _get(self, path, params=None, auth=False):
        if path == "/api/v1/contracts/active":
            return [{"symbol": "ADAUSDTM", "maintainMargin": "0.0065"}]
        if path == "/api/v1/account-overview":
            self.events.append("account")
            return {"marginBalance": "18.5482"}
        if path == "/api/v2/batchGetCrossOrderLimit":
            self.events.append("cross")
            if self.fail_cross:
                raise RuntimeError("unavailable")
            return [{
                "symbol": "ADAUSDTM",
                "mmr": "0.006691",
                "imr": "0.02",
                "totalMargin": "18.5482",
                "price": "0.2058",
                "leverage": str(cfg.LEVERAGE),
            }]
        raise AssertionError(path)


def _engine_class(scoring):
    class Engine:
        def __init__(self, client, events):
            self.paper_trade = False
            self.pilot = _Pilot()
            self.client = client
            self.instruments = {"ADAUSDT": {"kucoinSymbol": "ADAUSDTM"}}
            self.events = events

        async def _open(self, sig):
            # In production this point represents the earlier NEXUS approval
            # and sizing work. CROSS risk must not be requested before it.
            self.events.append("nexus_approved_and_sized")
            return await scoring.calculate(
                sig.symbol, sig.direction,
                [1.0, 0.9], [1.1, 1.0], [0.8, 0.7], [10.0, 12.0],
                self.client,
            )

    return Engine


def test_cross_mmr_private_call_is_deferred_until_post_nexus_score_boundary():
    events = []
    _Scoring.events = events
    scoring = type("Scoring", (), {})()
    scoring.calculate = _Scoring.calculate
    liquidation = _Liquidation()
    Client = type("Client", (_Client,), {"_official_contract_mmr_installed": False})
    Engine = _engine_class(scoring)

    hardening.install(Client, Engine, scoring, liquidation, _Log())
    client = Client(events)
    result = asyncio.run(Engine(client, events)._open(_Signal()))

    assert events == ["nexus_approved_and_sized", "account", "cross", "legacy_score"]
    assert result["total"] == 54
    assert liquidation.get_mmr("ADAUSDT") == (0.006691, True)


def test_cross_mmr_failure_returns_explicit_hard_block_before_legacy_score():
    events = []
    _Scoring.events = events
    scoring = type("Scoring", (), {})()
    scoring.calculate = _Scoring.calculate
    liquidation = _Liquidation()
    Client = type("ClientFail", (_Client,), {"_official_contract_mmr_installed": False})
    Engine = _engine_class(scoring)

    hardening.install(Client, Engine, scoring, liquidation, _Log())
    client = Client(events, fail_cross=True)
    result = asyncio.run(Engine(client, events)._open(_Signal()))

    assert events == ["nexus_approved_and_sized", "account", "cross"]
    assert result["aprovado"] is False
    assert result["hard_block"] == "official_cross_mmr_unavailable"
