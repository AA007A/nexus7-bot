"""Correct trailing-stop lock geometry.

The legacy implementation multiplied peak price by TRAILING_LOCK*0.1, which
turned a 25% lock setting into a 2.5% price offset. At 50x that is economically
very different from locking a fraction of the favorable excursion.

This hardening interprets TRAILING_LOCK as the fraction of peak favorable
excursion that may be given back. With 0.25, the stop retains 75% of MFE.
Native protective SL/TP and exchange routing are untouched.
"""
from __future__ import annotations


def install(Position, cfg, log) -> None:
    if getattr(Position, "_trailing_safety_hardening_installed", False):
        return

    def calc_trailing_sl(self):
        if self.pnl <= 0 or self.tp == self.entry or self.qty <= 0:
            return None

        target = abs(self.tp - self.entry)
        if target <= 0:
            return None

        trigger_pnl = target * cfg.TRAILING_TRIGGER * self.qty
        if self.pnl < trigger_pnl:
            return None

        self.trailing_active = True
        giveback = max(0.0, min(1.0, float(cfg.TRAILING_LOCK)))
        favorable_excursion = self.peak_pnl / self.qty
        retained_excursion = favorable_excursion * (1.0 - giveback)

        if self.direction == "LONG":
            new_sl = self.entry + retained_excursion
            return max(new_sl, self.sl)

        new_sl = self.entry - retained_excursion
        return min(new_sl, self.sl)

    Position.calc_trailing_sl = calc_trailing_sl
    Position._trailing_safety_hardening_installed = True
    log.warning(
        "[TRAILING_SAFETY] installed semantics=peak_excursion_giveback "
        "native_sl_tp_unchanged=true thresholds_unchanged=true leverage_unchanged=true"
    )
