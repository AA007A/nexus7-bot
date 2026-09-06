"""Startup audit for silent exception handlers.

Observability only. It does not change trading decisions, risk, execution,
network behavior, or exception semantics. It enumerates handlers whose body is
only ``pass`` so they can be reviewed explicitly instead of being summarized
as an opaque count by selfcheck.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

_BOT_ROOT = Path(__file__).resolve().parent


def _context_text(path: str, node: ast.ExceptHandler) -> str:
    """Return enough preceding source to classify the protected operation."""
    try:
        src = Path(path).read_text(encoding="utf-8").splitlines()
        start = max(0, (getattr(node, "lineno", 1) or 1) - 32)
        end = min(len(src), (getattr(node, "end_lineno", node.lineno) or node.lineno) + 2)
        return " ".join(src[start:end]).lower()
    except Exception as exc:
        return f"audit_read_error:{type(exc).__name__}"


def _classify(path: str, node: ast.ExceptHandler) -> str:
    """Conservative static triage for review priority, not an execution gate."""
    name = os.path.basename(path)
    text = _context_text(path, node)

    critical_terms = (
        "place_order", "set_sl", "set_position_stops", "save_trade", "database",
        "nexus", "execution_allowed", "risk", "integrity", "position", "reconcile",
        "balance", "websocket", "orderregistry", "fill",
    )
    best_effort_terms = (
        "notify", "telegram", "metrics", "log.debug", "observability", "heartbeat",
    )

    # engine.py contains tiny handlers around scheduling Telegram notifications
    # after the primary failure has already been logged CRITICAL/ERROR. The
    # protected operation is observability-only and must not affect engine/risk
    # execution. Use a wider context window so long multiline notification text
    # cannot be misclassified as REVIEW_MEDIUM merely because create_task is
    # more than eight lines above the except clause.
    notification_only_terms = (
        "asyncio.create_task(notify(",
        "create_task(notify(",
    )
    if any(t in text for t in notification_only_terms):
        return "BEST_EFFORT_LIKELY"

    if any(t in text for t in critical_terms):
        return "REVIEW_HIGH"
    if any(t in text for t in best_effort_terms):
        return "BEST_EFFORT_LIKELY"
    if name in {"logger.py", "notifier.py"}:
        return "BEST_EFFORT_LIKELY"
    return "REVIEW_MEDIUM"


def _justification(path: str, node: ast.ExceptHandler, priority: str) -> str:
    if priority != "BEST_EFFORT_LIKELY":
        return ""
    name = os.path.basename(path)
    text = _context_text(path, node)
    if name == "engine.py" and (
        "asyncio.create_task(notify(" in text or "create_task(notify(" in text
    ):
        return (
            "Telegram notification scheduling only; primary failure is already "
            "logged and trading/risk state must not depend on notification delivery"
        )
    if name == "score.py":
        try:
            exc_type = ast.unparse(node.type) if node.type is not None else ""
        except Exception:
            exc_type = ""
        if exc_type == "_SkipSource":
            return (
                "_SkipSource is an internal control-flow sentinel used to skip an "
                "unavailable optional score source; it is not an execution failure"
            )
    return "non-critical observability/best-effort handler"


def audit_silent_excepts(log):
    root = _BOT_ROOT
    rows = []
    paths = {}
    for candidate in sorted(root.iterdir()):
        if candidate.suffix != ".py":
            continue
        filename = candidate.name
        path = str(candidate)
        try:
            tree = ast.parse(candidate.read_text(encoding="utf-8"), filename=path)
        except Exception as exc:
            log.warning("[SILENT_EXCEPT_AUDIT] parse_failed file=%s error=%s", filename, exc)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            meaningful = [stmt for stmt in node.body if not isinstance(stmt, ast.Pass)]
            if meaningful:
                continue
            exc_type = "bare"
            if node.type is not None:
                try:
                    exc_type = ast.unparse(node.type)
                except Exception as exc:
                    exc_type = f"{type(node.type).__name__}:{type(exc).__name__}"
            priority = _classify(path, node)
            rows.append((filename, node.lineno, exc_type, priority))
            paths[(filename, node.lineno)] = (path, node)

    counts = {}
    for _, _, _, priority in rows:
        counts[priority] = counts.get(priority, 0) + 1
    log.info("[SILENT_EXCEPT_AUDIT] total=%d triage=%s", len(rows), counts)
    for filename, lineno, exc_type, priority in rows:
        log.warning(
            "[SILENT_EXCEPT] priority=%s file=%s line=%d type=%s",
            priority, filename, lineno, exc_type,
        )
        path, node = paths[(filename, lineno)]
        justification = _justification(path, node, priority)
        if justification:
            log.info(
                "[SILENT_EXCEPT_JUSTIFIED] file=%s line=%d reason=%s",
                filename, lineno, justification,
            )
    return rows
