import asyncio
from types import SimpleNamespace

from bot import runtime_overlays


class Log:
    def info(self, *a, **k): pass
    def debug(self, *a, **k): pass


class Analyzer:
    def analyze_mtf(self, *a, **k):
        return "production-result"


class Engine:
    paper_trade = False
    client = object()

    async def _nexus_validate(self, sig):
        return SimpleNamespace(execution_allowed=True, to_dict=lambda: {})

    def get_status(self):
        return {"base": True}


class NP:
    _single_conn_serialized = False
    @staticmethod
    async def _execute(sql, params=()): return 1
    @staticmethod
    async def _fetchall(sql, params=()): return []
    @staticmethod
    async def record_decision(sig, dec): return None
    @staticmethod
    async def evaluate_pending(client): return None
    @staticmethod
    def get_cached_metrics(): return {"ok": True}


class FM:
    @staticmethod
    def install(log): pass
    @staticmethod
    def get_funnel_metrics(): return {"ok": True}


class MS:
    @staticmethod
    def snapshot(): return {"unique_states": 0}
    @staticmethod
    def observe(*a, **k): return None


class Zero:
    @staticmethod
    def observe(*a, **k): return None


class Dedupe:
    @staticmethod
    def snapshot(): return {"ok": True}


class Notifier:
    @staticmethod
    async def notify_nexus(*a, **k): return None


class LoggerModule:
    _audit_pacing_patched = False
    @staticmethod
    def _enqueue(text): return text


def _install():
    runtime_overlays.install(
        TradingEngine=Engine, Analyzer=Analyzer, nexus_persistence=NP,
        funnel_metrics=FM, mtf_shadow=MS, nexus_zero_observability=Zero,
        nexus_decision_dedupe=Dedupe, notifier=Notifier,
        logger_module=LoggerModule, log=Log(),
    )


def test_install_is_idempotent_and_preserves_analyzer_result():
    _install()
    first = Analyzer.analyze_mtf
    _install()
    assert Analyzer.analyze_mtf is first
    assert Analyzer().analyze_mtf("X", [], [], []) == "production-result"
    assert NP._single_conn_serialized is True
    assert LoggerModule._audit_pacing_patched is True


def test_validate_returns_original_decision_and_status_is_additive():
    _install()
    eng = Engine()
    dec = asyncio.run(eng._nexus_validate(SimpleNamespace()))
    assert dec.execution_allowed is True
    status = eng.get_status()
    assert status["base"] is True
    assert status["nexus_persistent_metrics"] == {"ok": True}
    assert status["funnel_metrics"] == {"ok": True}
    assert status["mtf_shadow_metrics"] == {"unique_states": 0}
