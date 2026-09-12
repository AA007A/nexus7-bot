import inspect

from bot import missed_opportunity_audit as audit


class _Log:
    def __init__(self):
        self.lines = []

    def debug(self, message, *args):
        self.lines.append(message % args if args else message)


def test_valid_metadata_blocker_remains_authoritative():
    log = _Log()
    assert audit._metadata_blocker('{"blocker_class":"EV_RR"}', "OTHER", log) == "EV_RR"
    assert log.lines == []


def test_invalid_json_uses_reason_fallback_and_logs_observability():
    log = _Log()
    blocker = audit._metadata_blocker("{bad-json", "MTF", log)
    assert blocker == "MTF"
    assert any("metadata_parse_fallback" in line for line in log.lines)
    assert any("execution_effect=NONE" in line for line in log.lines)


def test_non_object_json_uses_reason_fallback_instead_of_crashing_row():
    log = _Log()
    blocker = audit._metadata_blocker("[]", "REGIME", log)
    assert blocker == "REGIME"
    assert any("metadata_type_fallback" in line for line in log.lines)
    assert any("type=list" in line for line in log.lines)


def test_null_or_missing_blocker_uses_reason_fallback():
    log = _Log()
    assert audit._metadata_blocker(None, "SCORE", log) == "SCORE"
    assert audit._metadata_blocker("{}", "DATA", log) == "DATA"


def test_opportunity_metadata_fallback_does_not_mutate_execution_controls():
    source = inspect.getsource(audit)
    assert "metadata_parse_fallback" in source
    assert "metadata_type_fallback" in source
    assert "except (TypeError, ValueError):\n                    pass" not in source
    forbidden = (
        "place_order", "create_order", "cancel_order", "close_position",
        "cfg.LEVERAGE =", "NEXUS_MIN_RR_NET =", "MIN_ENTRY_SCORE =",
        "MIN_VOLUME_MULT =",
    )
    assert all(token not in source for token in forbidden)
