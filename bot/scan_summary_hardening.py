"""Runtime hardening for validation observability.

Keeps scan summaries unique per symbol, clarifies that their count is the
score-stage count (not the monitored universe), prevents an uninitialized
zero daily target from being treated as achieved, and teaches the static
self-check about modules intentionally loaded by root sitecustomize.py.

No trading decision, threshold, risk parameter or execution gate is changed.
"""
import logging
import os
import re


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
    """Make score-stage telemetry explicit without altering scan logic."""

    def filter(self, record):
        try:
            msg = record.getMessage()
            if msg.startswith("🔎 SCAN:"):
                msg = msg.replace("🔎 SCAN:", "🔎 SCORE_STAGE:", 1)
                marker = " pares |"
                if marker in msg:
                    msg = msg.replace(marker, " pares com score registrado |", 1)
                record.msg = msg
                record.args = ()
        except Exception as exc:
            # Never hide an observability formatting failure. Mutate the same
            # record instead of logging recursively from inside a logging filter.
            record.msg = (
                f"[SCAN_SUMMARY_FILTER_ERROR] {type(exc).__name__}: {exc}"
            )
            record.args = ()
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


def _install_selfcheck_sitecustomize_awareness(log):
    """Suppress only orphan warnings disproved by explicit sitecustomize imports."""
    try:
        from bot import selfcheck
    except Exception as exc:
        log.debug(
            "[SELFCHECK_RUNTIME_IMPORTS] selfcheck import unavailable: %s: %s",
            type(exc).__name__, exc,
        )
        return

    if getattr(selfcheck, "_sitecustomize_imports_patched", False):
        return

    original = selfcheck.check_orphan_modules

    def check_orphan_modules_with_runtime_entrypoint(paths):
        issues = original(paths)
        try:
            # selfcheck already owns the canonical project root. Reuse it
            # instead of relying on __file__, which its intentionally strict
            # undefined-name analyzer does not model as an implicit builtin.
            root = selfcheck._ROOT
            entrypoint = os.path.join(root, "sitecustomize.py")
            with open(entrypoint, encoding="utf-8") as fh:
                src = fh.read()
        except Exception as exc:
            log.debug(
                "[SELFCHECK_RUNTIME_IMPORTS] entrypoint scan failed: %s: %s",
                type(exc).__name__, exc,
            )
            return issues

        imported = set(re.findall(r"\bfrom\s+bot\s+import\s+([A-Za-z_][A-Za-z0-9_]*)", src))
        imported.update(re.findall(r"\bfrom\s+bot\.([A-Za-z_][A-Za-z0-9_]*)\s+import\b", src))
        imported.update(re.findall(r"\bimport\s+bot\.([A-Za-z_][A-Za-z0-9_]*)\b", src))

        if not imported:
            return issues

        kept = []
        for issue in issues:
            filename = issue.split(" ", 1)[0]
            stem = filename[:-3] if filename.endswith(".py") else filename
            if stem in imported:
                continue
            kept.append(issue)
        return kept

    selfcheck.check_orphan_modules = check_orphan_modules_with_runtime_entrypoint
    selfcheck._sitecustomize_imports_patched = True
    log.info("[SELFCHECK] root sitecustomize imports recognized as active runtime references")


def install(log):
    from bot import strategy

    if getattr(strategy, "_scan_summary_unique_patched", False):
        return

    def get_score_log_unique(limit=300):
        return latest_unique(getattr(strategy, "_SCORE_LOG", []), limit)

    strategy.get_score_log = get_score_log_unique
    strategy._scan_summary_unique_patched = True

    try:
        for handler in getattr(log, "handlers", []):
            if isinstance(handler, logging.StreamHandler):
                handler.addFilter(_ScanSummaryLabelFilter())
    except Exception as exc:
        log.debug(
            "[SCAN_SUMMARY] filter installation failed: %s: %s",
            type(exc).__name__, exc,
        )

    _install_daily_target_guard(log)
    _install_selfcheck_sitecustomize_awareness(log)

    log.info(
        "[SCAN_SUMMARY] latest-per-symbol score-stage view enabled; trading logic unchanged"
    )
