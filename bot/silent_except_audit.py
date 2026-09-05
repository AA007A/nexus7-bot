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


def _classify(path: str, node: ast.ExceptHandler) -> str:
    """Conservative static triage for review priority, not an execution gate."""
    name = os.path.basename(path)
    text = ""
    try:
        src = Path(path).read_text(encoding="utf-8").splitlines()
        start = max(0, (getattr(node, "lineno", 1) or 1) - 8)
        end = min(len(src), (getattr(node, "end_lineno", node.lineno) or node.lineno) + 2)
        text = " ".join(src[start:end]).lower()
    except Exception as exc:
        text = f"audit_read_error:{type(exc).__name__}"

    critical_terms = (
        "place_order", "set_sl", "set_position_stops", "save_trade", "database",
        "nexus", "execution_allowed", "risk", "integrity", "position", "reconcile",
        "balance", "websocket", "orderregistry", "fill",
    )
    best_effort_terms = (
        "notify", "telegram", "metrics", "log.debug", "observability", "heartbeat",
    )

    # A very common pattern in engine.py is a tiny try/except whose only work is
    # scheduling a Telegram notification after the real error has already been
    # logged. Nearby context can mention positions/risk and previously caused a
    # false REVIEW_HIGH. Match the operation itself before broad context terms.
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


def audit_silent_excepts(log):
    root = Path(__file__).resolve().parent
    rows = []
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
            rows.append((filename, node.lineno, exc_type, _classify(path, node)))

    counts = {}
    for _, _, _, priority in rows:
        counts[priority] = counts.get(priority, 0) + 1
    log.info("[SILENT_EXCEPT_AUDIT] total=%d triage=%s", len(rows), counts)
    for filename, lineno, exc_type, priority in rows:
        log.warning(
            "[SILENT_EXCEPT] priority=%s file=%s line=%d type=%s",
            priority, filename, lineno, exc_type,
        )
    return rows
