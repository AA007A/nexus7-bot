"""Runtime hardening for scan observability and daily-target initialization.

The strategy keeps an append-only score history. The engine asks for the last
N rows when printing a scan summary, which can return the same symbols several
times when only a few pairs reached the score stage in consecutive scans.
This module exposes the latest score per symbol while preserving the underlying
history and all trading decisions.

It also guards a startup-only DailyTracker edge case: when DAILY_TARGET is
configured dynamically (zero in config), check_limits() can run before a
positive balance has initialized the tracker target. In that state 0 >= 0 must
never be interpreted as a profit target hit.
"""
import logging


def latest_unique(records, limit):
    """Return newest record per symbol, newest first, capped to ``limit``."""
    try:
        cap = max(0, int(limit))
    except Exception:
        cap = 0
    if cap == 0:
        return []

    out = []
    seen = set()
    for row in reversed(list(records)):
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(dict(row))
        if len(out) >= cap:
            break
    return out


class _ScanSummaryLabelFilter(logging.Filter):
    """Make it explicit that SCAN rows count score-stage records, not universe size."""

    def filter(self, record):
        try:
            msg = record.getMessage()
            if msg.startswith("🔎 SCAN:"):
                # The count comes from strategy._SCORE_LOG. Symbols rejected
                # before score construction are still monitored/scanned, but
                # correctly do not have a score row. Avoid calling this count
                # the total number of monitored pairs.
                msg = msg.replace("🔎 SCAN:", "🔎 SCORE_STAGE:", 1)
                marker = " pares |"
                if marker in msg:
                    msg = msg.replace(marker, " pares com score registrado |", 1)
                record.msg = msg
                record.args = ()
        except Exception:
            pass
        return True


def _install_daily_target_guard(log):
    """Prevent an uninitialized dynamic target (0) from becoming TARGET at PnL 0."""
    from bot.daily_tracker import DailyTracker

    if getattr(DailyTracker, "_positive_target_guard_patched", False):
        return

    original_check_limits = DailyTracker.check_limits

    def check_limits_positive_target(self):
        target = float(getattr(self, "daily_target", 0.0) or 0.0)
        if target > 0:
            return original_check_limits(self)

        # Preserve monthly/weekly stop evaluation in the original method while
        # making the not-yet-initialized daily target unreachable for this call.
        old_target = getattr(self, "daily_target", 0.0)
        self.daily_target = float("inf")
        try:
            return original_check_limits(self)
        finally:
            self.daily_target = old_target

    DailyTracker.check_limits = check_limits_positive_target
    DailyTracker._positive_target_guard_patched = True
    log.info(
        "[DAILY_TARGET] positive-target guard enabled; PnL=0 cannot hit an uninitialized target"
    )


def install(log):
    from bot import strategy

    if getattr(strategy, "_scan_summary_unique_patched", False):
        return

    def get_score_log_unique(limit=300):
        return latest_unique(getattr(strategy, "_SCORE_LOG", []), limit)

    strategy.get_score_log = get_score_log_unique
    strategy._scan_summary_unique_patched = True

    # Clarify the existing INFO summary without touching scan/entry logic.
    try:
        for handler in getattr(log, "handlers", []):
            if isinstance(handler, logging.StreamHandler):
                handler.addFilter(_ScanSummaryLabelFilter())
    except Exception:
        pass

    _install_daily_target_guard(log)

    log.info(
        "[SCAN_SUMMARY] latest-per-symbol score-stage view enabled; trading logic unchanged"
    )
