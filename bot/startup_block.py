"""Startup integrity block classification and Telegram-safe diagnostics.

Pure helpers only: no exchange, network, credentials, or trading side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class StartupBlock:
    code: str
    detail: str


def classify_startup_block(
    *,
    sitecustomize_status: str,
    critical_issues: Optional[Iterable[str]] = None,
    selfcheck_error: Optional[BaseException] = None,
) -> Optional[StartupBlock]:
    """Return the exact fail-closed startup reason, or None when healthy.

    Priority is deterministic: hardening confirmation, self-check execution,
    then structural critical findings. Warnings are deliberately absent from
    this API and therefore cannot block startup.
    """
    if sitecustomize_status != "ok":
        return StartupBlock(
            "SITECUSTOMIZE_NOT_CONFIRMED",
            f"startup hardenings not confirmed (sitecustomize={sitecustomize_status})",
        )
    if selfcheck_error is not None:
        return StartupBlock(
            "SELFCHECK_EXCEPTION",
            f"self-check could not confirm code integrity: {type(selfcheck_error).__name__}",
        )
    issues = tuple(critical_issues or ())
    if issues:
        return StartupBlock(
            "SELFCHECK_CRITICAL",
            f"{len(issues)} structural critical finding(s)",
        )
    return None


def telegram_block_message(block: StartupBlock, startup_id: str = "unknown") -> str:
    """Build an unambiguous operator alert without leaking exception details."""
    return (
        "🚫 *BOT BLOQUEADO NO STARTUP*\n"
        f"Código: `{block.code}`\n"
        f"Startup: `{startup_id}`\n"
        f"Causa: {block.detail}\n"
        "Nenhuma ordem será enviada enquanto o bloqueio estiver ativo.\n"
        "_Verifique os logs do Railway para o mesmo Startup ID._"
    )
