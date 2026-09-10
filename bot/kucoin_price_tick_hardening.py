"""Exact KuCoin price-tick quantization hardening.

The core client historically derived output precision from log10(tickSize),
which is only correct for power-of-ten ticks. Instruments with increments such
as 0.25, 0.5 or 0.025 could therefore produce an invalid exchange price even
after selecting the correct tick count.

This module installs an exact Decimal-based replacement for ``_round_price``.
It changes formatting/quantization only; it does not submit orders, alter
positions, change risk limits, or grant execution permission.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _decimal_text(value) -> str:
    """Convert float/string/numeric input without binary-float expansion."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid price/tick")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid decimal value") from exc
    if not dec.is_finite():
        raise ValueError("non-finite decimal value")
    return str(dec)


def quantize_price_to_tick(price, tick_size) -> str:
    """Round ``price`` to the nearest exact multiple of ``tick_size``.

    KuCoin accepts decimal strings aligned to the contract tick. Using Decimal
    avoids both binary floating-point drift and the incorrect assumption that
    every tick is a power of ten.
    """
    p = Decimal(_decimal_text(price))
    tick = Decimal(_decimal_text(tick_size))
    if p <= 0:
        raise ValueError("price must be positive")
    if tick <= 0:
        raise ValueError("tick_size must be positive")

    ticks = (p / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    clean = ticks * tick

    # Preserve the scale required by the tick itself, then strip only surplus
    # trailing zeroes. This yields exchange-safe strings such as 1.25, 1.250,
    # 1.5, or 0.001 as appropriate without scientific notation.
    tick_places = max(0, -tick.as_tuple().exponent)
    text = f"{clean:.{tick_places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def install(KuCoinClient, log) -> None:
    if getattr(KuCoinClient, "_price_tick_hardening_patched", False):
        return

    def _round_price_exact(self, price: float, symbol: str) -> str:
        info = self._instruments.get(symbol, {})
        tick_size = info.get("tickSize", 0.01)
        try:
            return quantize_price_to_tick(price, tick_size)
        except ValueError as exc:
            # Invalid/missing instrument metadata must not silently fabricate an
            # exchange price. Callers will fail closed before any mutation.
            raise ValueError(
                f"invalid price quantization symbol={symbol}: {exc}"
            ) from exc

    KuCoinClient._round_price = _round_price_exact
    KuCoinClient._price_tick_hardening_patched = True
    log.info(
        "[KUCOIN_PRICE_TICK] exact Decimal tick quantization installed; "
        "execution_effect=NONE"
    )
