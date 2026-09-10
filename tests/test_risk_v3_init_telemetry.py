from unittest.mock import Mock, patch

from bot.professional_risk_adapter import ProfessionalRiskAdapter


class LegacyRiskStub:
    def __init__(self):
        self._ready = False
        self.balance = 0.0
        self.balance_confirmed = False
        self.peak_balance = 0.0
        self.drawdown = 0.0
        self.update_calls = 0

    def update(self, bal):
        self.update_calls += 1
        self.balance = float(bal)
        self.balance_confirmed = True
        self.peak_balance = max(self.peak_balance, self.balance)
        self.drawdown = 0.0


def test_adapter_init_preserves_legacy_state_without_legacy_risk_label():
    legacy = LegacyRiskStub()
    adapter = ProfessionalRiskAdapter(legacy)

    with patch("bot.professional_risk_adapter.log.info") as info:
        adapter.init(100.0)

    assert legacy._ready is True
    assert legacy.balance == 100.0
    assert legacy.balance_confirmed is True
    assert legacy.update_calls == 1
    rendered = " ".join(str(part) for call in info.call_args_list for part in call.args)
    assert "configured_stop_risk_pct" in rendered
    assert "risco_trade" not in rendered


def test_adapter_init_is_idempotent_like_legacy_initializer():
    legacy = LegacyRiskStub()
    adapter = ProfessionalRiskAdapter(legacy)
    adapter.init(100.0)
    adapter.init(90.0)
    assert legacy.update_calls == 1
    assert legacy.balance == 100.0
