import asyncio
from types import SimpleNamespace

from bot import restart_opening_order_lineage as lineage


class _Log:
    def __init__(self):
        self.events = []

    def warning(self, msg, *args):
        self.events.append(("warning", msg % args if args else msg))

    def critical(self, msg, *args):
        self.events.append(("critical", msg % args if args else msg))


class _Position:
    pass


def _engine_class(proof, *, existing_lineage=None):
    pos = _Position()
    if existing_lineage is not None:
        pos._forensic_lineage = dict(existing_lineage)

    class Engine:
        async def _load_existing_positions(self):
            self.positions = {"NEARUSDT": pos}
            self._recovered_position_symbols = {"NEARUSDT"}
            self._restart_ownership_proofs = {"NEARUSDT": proof}
            return "loaded"

    return Engine, pos


def test_restart_rehydrates_exact_opening_order_id_and_preserves_metadata():
    proof = SimpleNamespace(recovered=True, order_id="opening-order-123")
    Engine, pos = _engine_class(proof, existing_lineage={"nexus_source": "final_validation"})
    log = _Log()
    lineage.install(Engine, log)

    result = asyncio.run(Engine()._load_existing_positions())

    assert result == "loaded"
    assert pos._forensic_lineage["opening_order_id"] == "opening-order-123"
    assert pos._forensic_lineage["nexus_source"] == "final_validation"
    assert any("result=RESTORED" in event for _, event in log.events)


def test_restart_never_invents_missing_opening_order_id():
    proof = SimpleNamespace(recovered=True, order_id="")
    Engine, pos = _engine_class(proof)
    log = _Log()
    lineage.install(Engine, log)

    asyncio.run(Engine()._load_existing_positions())

    assert not hasattr(pos, "_forensic_lineage")
    assert any("missing_exact_opening_order_id" in event for _, event in log.events)


def test_restart_never_overwrites_conflicting_opening_order_id():
    proof = SimpleNamespace(recovered=True, order_id="authoritative-order")
    Engine, pos = _engine_class(
        proof, existing_lineage={"opening_order_id": "different-order"}
    )
    log = _Log()
    lineage.install(Engine, log)

    asyncio.run(Engine()._load_existing_positions())

    assert pos._forensic_lineage["opening_order_id"] == "different-order"
    assert any("opening_order_id_conflict" in event for _, event in log.events)


def test_unrecovered_symbol_is_not_mutated():
    pos = _Position()

    class Engine:
        async def _load_existing_positions(self):
            self.positions = {"NEARUSDT": pos}
            self._recovered_position_symbols = set()
            self._restart_ownership_proofs = {}

    log = _Log()
    lineage.install(Engine, log)
    asyncio.run(Engine()._load_existing_positions())

    assert not hasattr(pos, "_forensic_lineage")
