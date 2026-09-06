"""Temporary validation safety lock and production CI release gate.

While NEXUS-7 is undergoing PAPER validation, any runtime that starts with
PAPER_TRADE disabled must fail closed before exchange-facing engine activity.
In Railway production, engine.run also waits for the GitHub `Quality Check`
push workflow for the exact deployed commit to complete successfully.

The HTTP server remains available while the CI result is pending, but the
trading engine is not started until the release gate is satisfied. A missing,
failed, cancelled, timed-out, or unverifiable Quality Check fails closed.

Remove this module from sitecustomize only after validation is complete and a
separate, explicit live-readiness decision is made.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import aiohttp

_REPO = "AA007A/nexus7-bot"
_WORKFLOW_NAME = "Quality Check"
_POLL_SECONDS = 10.0
_MAX_WAIT_SECONDS = 300.0


def _is_railway_production() -> bool:
    return bool(os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip()) and (
        os.environ.get("RAILWAY_ENVIRONMENT_NAME", "").strip().lower() == "production"
    )


def _quality_state(payload: dict[str, Any], sha: str) -> tuple[str, str]:
    """Return (state, detail) for the exact main push Quality Check run.

    state is one of: success, pending, failed, missing.
    """
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        return "failed", "invalid GitHub Actions response schema"

    matching = [
        run for run in runs
        if isinstance(run, dict)
        and run.get("name") == _WORKFLOW_NAME
        and run.get("event") == "push"
        and run.get("head_sha") == sha
    ]
    if not matching:
        return "missing", "Quality Check push run not found for deployed commit"

    # GitHub returns newest runs first. Exact-SHA duplicates are unusual but
    # selecting the newest prevents an older rerun from masking current state.
    run = matching[0]
    status = str(run.get("status") or "").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    run_id = run.get("id")

    if status != "completed":
        return "pending", f"run={run_id} status={status or 'unknown'}"
    if conclusion == "success":
        return "success", f"run={run_id} conclusion=success"
    return "failed", f"run={run_id} conclusion={conclusion or 'unknown'}"


async def _fetch_quality_payload(sha: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{_REPO}/actions/runs"
    params = {"head_sha": sha, "branch": "main", "per_page": "20"}
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "nexus7-runtime-release-gate",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, params=params) as response:
            if response.status != 200:
                body = (await response.text())[:240]
                raise RuntimeError(f"GitHub Actions HTTP {response.status}: {body}")
            payload = await response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("GitHub Actions response is not an object")
            return payload


async def _wait_for_quality_gate(log) -> bool:
    """Fail closed until the exact deployed commit has a successful push QC."""
    if not _is_railway_production():
        return True

    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip()
    deadline = asyncio.get_running_loop().time() + _MAX_WAIT_SECONDS
    last_detail = "not checked"

    log.warning(
        "[CI_RELEASE_GATE] production commit=%s waiting for GitHub Quality Check",
        sha[:12],
    )

    while asyncio.get_running_loop().time() < deadline:
        try:
            payload = await _fetch_quality_payload(sha)
            state, detail = _quality_state(payload, sha)
            last_detail = detail
            if state == "success":
                log.info("[CI_RELEASE_GATE] PASS commit=%s %s", sha[:12], detail)
                return True
            if state == "failed":
                log.critical(
                    "[CI_RELEASE_GATE] BLOCKED commit=%s %s",
                    sha[:12], detail,
                )
                return False
            log.info(
                "[CI_RELEASE_GATE] waiting commit=%s state=%s %s",
                sha[:12], state, detail,
            )
        except Exception as exc:
            # Verification failure is never permission to start the engine.
            last_detail = f"verification error: {type(exc).__name__}: {exc}"
            log.warning("[CI_RELEASE_GATE] %s", last_detail)

        await asyncio.sleep(_POLL_SECONDS)

    log.critical(
        "[CI_RELEASE_GATE] BLOCKED commit=%s timeout after %.0fs; last=%s",
        sha[:12], _MAX_WAIT_SECONDS, last_detail,
    )
    return False


def install(log):
    from bot.engine import TradingEngine

    if getattr(TradingEngine, "_validation_safety_lock_patched", False):
        return

    original_run = TradingEngine.run
    original_connect = TradingEngine._connect
    original_open = TradingEngine._open
    original_sync = TradingEngine._sync_positions
    original_reconcile = getattr(TradingEngine, "_reconcile_exchange_positions", None)
    original_guard = getattr(TradingEngine, "_guard_naked_positions", None)

    async def _run_release_gated(self, *args, **kwargs):
        if not await _wait_for_quality_gate(log):
            self.active = False
            self.connected = False
            log.critical(
                "[CI_RELEASE_GATE] engine not started because deployed commit "
                "has no verified successful Quality Check"
            )
            return None
        return await original_run(self, *args, **kwargs)

    async def _connect_locked(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_connect(self, *args, **kwargs)
        self.active = False
        self.connected = False
        log.critical(
            "[VALIDATION_LOCK] LIVE runtime blocked: PAPER validation is not complete; "
            "no exchange-facing engine connection will be started"
        )
        return False

    async def _open_locked(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_open(self, *args, **kwargs)
        log.critical("[VALIDATION_LOCK] real order opening blocked")
        return None

    async def _sync_locked(self, *args, **kwargs):
        if getattr(self, "paper_trade", False):
            return await original_sync(self, *args, **kwargs)
        return None

    TradingEngine.run = _run_release_gated
    TradingEngine._connect = _connect_locked
    TradingEngine._open = _open_locked
    TradingEngine._sync_positions = _sync_locked

    if original_reconcile is not None:
        async def _reconcile_locked(self, *args, **kwargs):
            if getattr(self, "paper_trade", False):
                return await original_reconcile(self, *args, **kwargs)
            return []
        TradingEngine._reconcile_exchange_positions = _reconcile_locked

    if original_guard is not None:
        async def _guard_locked(self, *args, **kwargs):
            if getattr(self, "paper_trade", False):
                return await original_guard(self, *args, **kwargs)
            return None
        TradingEngine._guard_naked_positions = _guard_locked

    TradingEngine._validation_safety_lock_patched = True
    log.warning(
        "[VALIDATION_LOCK] installed: PAPER unaffected; LIVE engine activity blocked; "
        "Railway production engine startup requires successful Quality Check"
    )
