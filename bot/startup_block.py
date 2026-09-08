"""Startup integrity block classification and startup diagnostics.

This module contains the fail-closed startup classifier plus a one-shot,
observational static diagnostic. The diagnostic has no exchange/network access
and cannot grant or revoke trading permission; classification remains driven
only by the explicit inputs passed to ``classify_startup_block``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class StartupBlock:
    code: str
    detail: str


_startup_diagnostics_done = False


def run_startup_diagnostics_once() -> bool:
    """Run static observability once from the explicit application startup path.

    Failures are diagnostic-only and never alter startup classification. This
    deliberately replaces the previous implicit invocation from sitecustomize.
    """
    global _startup_diagnostics_done
    if _startup_diagnostics_done:
        return True
    _startup_diagnostics_done = True
    try:
        from bot.logger import log
        from bot.silent_except_audit import audit_silent_excepts

        audit_silent_excepts(log)
        log.info(
            "[STARTUP_DIAGNOSTICS] silent-except audit source=explicit_startup "
            "decision_effect=NONE execution_effect=NONE"
        )
        return True
    except Exception as exc:
        try:
            from bot.logger import log
            log.warning(
                "[STARTUP_DIAGNOSTICS] silent-except audit failed error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )
        except Exception:
            return False
        return False


def classify_startup_block(
    *,
    sitecustomize_status: str,
    critical_issues: Optional[Iterable[str]] = None,
    selfcheck_error: Optional[BaseException] = None,
) -> Optional[StartupBlock]:
    """Return the exact fail-closed startup reason, or None when healthy.

    Priority is deterministic: hardening confirmation, self-check execution,
    then structural critical findings. Warnings are deliberately absent from
    this API and therefore cannot block startup. Static diagnostics are invoked
    here for observability but their result is intentionally not an input to the
    classifier.
    """
    run_startup_diagnostics_once()

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
