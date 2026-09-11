import asyncio

from bot import legacy_pretrade_advisory as advisory


class _Log:
    def warning(self, *args, **kwargs):
        pass


class _Pilot:
    enabled = True


class _Scoring:
    MIN_SCORE = 60

    @staticmethod
    async def calculate(symbol, direction, closes, highs, lows, volumes, client=None):
        return {
            "total": 54,
            "tecnico": 20,
            "orderflow": 23,
            "macro": 11,
            "news_mod": 0,
            "aprovado": False,
        }


class _Signal:
    symbol = "ADAUSDT"
    direction = "SHORT"
    entry = 0.2036
    sl = 0.2068
    tp = 0.1972


def _approved_decision(**overrides):
    out = {
        "symbol": "ADAUSDT",
        "decision": "SHORT",
        "entry": 0.2036,
        "stop_loss": 0.2068,
        "take_profit": 0.1972,
        "setup_quality": 71.65,
        "execution_allowed": True,
    }
    out.update(overrides)
    return out


def _engine_class(*, paper=False, decision=None):
    scoring = type("Scoring", (), {})()
    scoring.MIN_SCORE = _Scoring.MIN_SCORE
    scoring.calculate = _Scoring.calculate

    class Engine:
        _legacy_pretrade_advisory_installed = False

        def __init__(self):
            self.paper_trade = paper
            self.pilot = _Pilot()
            self._last_nexus = {
                "ADAUSDT": decision if decision is not None else _approved_decision()
            }
            self.result = None

        async def _open(self, sig):
            self.result = await scoring.calculate(
                sig.symbol,
                sig.direction,
                [1.0, 0.9],
                [1.1, 1.0],
                [0.8, 0.7],
                [10.0, 12.0],
                None,
            )
            return self.result

    return Engine, scoring


def test_exact_nexus_approval_makes_legacy_soft_score_advisory():
    Engine, scoring = _engine_class()
    advisory.install(Engine, scoring, _Log())
    result = asyncio.run(Engine()._open(_Signal()))

    assert result["total"] == 54
    assert result["legacy_aprovado"] is False
    assert result["legacy_gate_mode"] == "ADVISORY_AFTER_EXACT_NEXUS_APPROVAL"
    assert result["aprovado"] is True


def test_negative_nexus_decision_keeps_legacy_gate_authoritative():
    Engine, scoring = _engine_class(
        decision=_approved_decision(execution_allowed=False)
    )
    advisory.install(Engine, scoring, _Log())
    result = asyncio.run(Engine()._open(_Signal()))
    assert result["aprovado"] is False
    assert "legacy_gate_mode" not in result


def test_stale_or_mismatched_nexus_levels_cannot_bypass_legacy_gate():
    Engine, scoring = _engine_class(
        decision=_approved_decision(entry=0.2040)
    )
    advisory.install(Engine, scoring, _Log())
    result = asyncio.run(Engine()._open(_Signal()))
    assert result["aprovado"] is False
    assert "legacy_gate_mode" not in result


def test_wrong_side_cannot_bypass_legacy_gate():
    Engine, scoring = _engine_class(
        decision=_approved_decision(decision="LONG")
    )
    advisory.install(Engine, scoring, _Log())
    result = asyncio.run(Engine()._open(_Signal()))
    assert result["aprovado"] is False


def test_explicit_hard_block_cannot_be_bypassed_by_exact_nexus_approval():
    Engine, scoring = _engine_class()

    async def hard_block(symbol, direction, closes, highs, lows, volumes, client=None):
        return {
            "total": 0,
            "tecnico": 0,
            "orderflow": 0,
            "macro": 0,
            "news_mod": 0,
            "aprovado": False,
            "hard_block": "official_cross_mmr_unavailable",
        }

    scoring.calculate = hard_block
    advisory.install(Engine, scoring, _Log())
    result = asyncio.run(Engine()._open(_Signal()))

    assert result["aprovado"] is False
    assert result["hard_block"] == "official_cross_mmr_unavailable"
    assert "legacy_gate_mode" not in result


def test_paper_path_is_unchanged():
    Engine, scoring = _engine_class(paper=True)
    advisory.install(Engine, scoring, _Log())
    result = asyncio.run(Engine()._open(_Signal()))
    assert result["aprovado"] is False
    assert "legacy_gate_mode" not in result


def test_scoring_outside_live_open_context_is_unchanged():
    Engine, scoring = _engine_class()
    advisory.install(Engine, scoring, _Log())
    result = asyncio.run(
        scoring.calculate(
            "ADAUSDT", "SHORT", [1, 0.9], [1.1, 1], [0.8, 0.7], [10, 12]
        )
    )
    assert result["aprovado"] is False
    assert "legacy_gate_mode" not in result
