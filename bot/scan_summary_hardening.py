"""Runtime hardening for scan-score observability.

The strategy keeps an append-only score history.  The engine asks for the last
N rows when printing a scan summary, which can return the same symbols several
times when only a few pairs reached the score stage in consecutive scans.
This module exposes the latest score per symbol while preserving the underlying
history and all trading decisions.
"""


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


def install(log):
    from bot import strategy

    if getattr(strategy, "_scan_summary_unique_patched", False):
        return

    def get_score_log_unique(limit=300):
        return latest_unique(getattr(strategy, "_SCORE_LOG", []), limit)

    strategy.get_score_log = get_score_log_unique
    strategy._scan_summary_unique_patched = True
    log.info(
        "[SCAN_SUMMARY] latest-per-symbol view enabled; trading logic unchanged"
    )
