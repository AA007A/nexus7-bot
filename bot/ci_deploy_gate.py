"""Fail-closed pre-deploy gate for Railway.

Railway can begin a deployment before the GitHub push workflow for the exact
main commit has finished when native check-suite waiting is disabled. This
script is intended to run as a Railway pre-deploy command. It waits for the
`Quality Check` workflow attached to RAILWAY_GIT_COMMIT_SHA and exits non-zero
unless that workflow completed successfully.

No exchange credentials are read and no trading code is imported.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = "AA007A/nexus7-bot"
WORKFLOW_PATH = "quality.yml"
WORKFLOW_NAME = "Quality Check"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_POLL_SECONDS = 15
DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 60
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class RateLimitError(RuntimeError):
    """GitHub API rate limit with an optional retry delay."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def validate_sha(value: str | None) -> str:
    sha = (value or "").strip()
    if not _SHA_RE.fullmatch(sha):
        raise ValueError("RAILWAY_GIT_COMMIT_SHA is missing or invalid")
    return sha.lower()


def classify_runs(payload: dict[str, Any], sha: str) -> tuple[str, str]:
    """Return (state, detail): success, pending, failed, or missing."""
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        return "failed", "GitHub response missing workflow_runs list"

    matching = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if str(run.get("head_sha", "")).lower() != sha:
            continue
        if run.get("name") != WORKFLOW_NAME:
            continue
        if run.get("event") != "push":
            continue
        matching.append(run)

    if not matching:
        return "missing", "no push Quality Check found for deployment commit"

    matching.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    run = matching[0]
    status = str(run.get("status", ""))
    conclusion = run.get("conclusion")

    if status != "completed":
        return "pending", f"Quality Check status={status or 'unknown'}"
    if conclusion == "success":
        return "success", "Quality Check completed successfully"
    return "failed", f"Quality Check completed with conclusion={conclusion!r}"


def _github_token() -> str:
    # Dedicated variable is preferred. GITHUB_TOKEN is supported as a
    # conventional fallback. Empty values are treated as unauthenticated.
    return (
        os.getenv("CI_DEPLOY_GATE_GITHUB_TOKEN")
        or os.getenv("GITHUB_TOKEN")
        or ""
    ).strip()


def _retry_after_from_http_error(exc: HTTPError) -> int | None:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after:
        try:
            return max(1, int(retry_after))
        except ValueError:
            pass

    reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
    if reset:
        try:
            return max(1, int(float(reset) - time.time()) + 1)
        except ValueError:
            pass
    return None


def fetch_runs(sha: str) -> dict[str, Any]:
    query = urlencode({"head_sha": sha, "event": "push", "per_page": 10})
    url = (
        f"https://api.github.com/repos/{REPO}/actions/workflows/"
        f"{WORKFLOW_PATH}/runs?{query}"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "nexus7-railway-ci-gate/1.1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=10) as response:  # nosec B310: fixed HTTPS host
            if response.status != 200:
                raise RuntimeError(f"GitHub API returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (403, 429):
            remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
            if exc.code == 429 or remaining == "0":
                raise RateLimitError(
                    f"GitHub API rate limited: HTTP {exc.code}",
                    retry_after=_retry_after_from_http_error(exc),
                ) from exc
        raise


def wait_for_quality_check(
    sha: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    last_detail = "not checked"

    while True:
        sleep_seconds = max(1, poll_seconds)
        try:
            state, detail = classify_runs(fetch_runs(sha), sha)
        except RateLimitError as exc:
            state = "pending"
            sleep_seconds = max(
                sleep_seconds,
                exc.retry_after or DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
            )
            detail = (
                f"GitHub API rate limited; retry_in={sleep_seconds}s; "
                f"authenticated={bool(_github_token())}"
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            state = "pending"
            detail = f"GitHub API unavailable: {type(exc).__name__}: {exc}"

        last_detail = detail
        print(f"[CI_DEPLOY_GATE] sha={sha[:12]} state={state} detail={detail}", flush=True)

        if state == "success":
            return True, detail
        if state == "failed":
            return False, detail

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, f"timeout waiting for Quality Check: {last_detail}"
        time.sleep(min(sleep_seconds, remaining))


def main() -> int:
    try:
        sha = validate_sha(os.getenv("RAILWAY_GIT_COMMIT_SHA"))
    except ValueError as exc:
        print(f"[CI_DEPLOY_GATE] BLOCKED: {exc}", file=sys.stderr, flush=True)
        return 2

    try:
        timeout_seconds = int(os.getenv("CI_DEPLOY_GATE_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS)))
        poll_seconds = int(os.getenv("CI_DEPLOY_GATE_POLL", str(DEFAULT_POLL_SECONDS)))
    except ValueError:
        print("[CI_DEPLOY_GATE] BLOCKED: invalid gate timing configuration", file=sys.stderr, flush=True)
        return 2

    if timeout_seconds < 1 or poll_seconds < 1:
        print("[CI_DEPLOY_GATE] BLOCKED: gate timings must be positive", file=sys.stderr, flush=True)
        return 2

    ok, detail = wait_for_quality_check(
        sha,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )
    if not ok:
        print(f"[CI_DEPLOY_GATE] BLOCKED: {detail}", file=sys.stderr, flush=True)
        return 3

    print(f"[CI_DEPLOY_GATE] PASSED: {detail}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
