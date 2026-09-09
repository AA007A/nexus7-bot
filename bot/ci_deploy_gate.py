"""Fail-closed pre-deploy gate for Railway.

Railway can begin a deployment before the GitHub push workflow for the exact
main commit has finished when native check-suite waiting is disabled. Instead
of polling the rate-limited GitHub REST API, this gate waits for an exact-SHA
attestation published by a separate ``workflow_run`` workflow only after the
main ``Quality Check`` workflow completed successfully.

No exchange credentials are read and no trading code is imported.
"""
from __future__ import annotations

import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO = "AA007A/nexus7-bot"
ATTESTATION_BRANCH = "ci-attestations"
ATTESTATION_BASE_URL = (
    f"https://raw.githubusercontent.com/{REPO}/{ATTESTATION_BRANCH}/passed"
)
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_POLL_SECONDS = 5
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def validate_sha(value: str | None) -> str:
    sha = (value or "").strip()
    if not _SHA_RE.fullmatch(sha):
        raise ValueError("RAILWAY_GIT_COMMIT_SHA is missing or invalid")
    return sha.lower()


def attestation_url(sha: str) -> str:
    """Return the immutable path used to attest one exact deployment SHA."""
    normalized = validate_sha(sha)
    return f"{ATTESTATION_BASE_URL}/{normalized}.txt"


def fetch_attestation(sha: str) -> tuple[str, str]:
    """Return ``(state, detail)`` for one exact-SHA CI attestation.

    ``success`` means the remote file exists and its body equals the exact SHA.
    ``pending`` means it has not been published yet. A present but mismatched
    file is a hard failure rather than a reason to keep polling.
    """
    normalized = validate_sha(sha)
    req = Request(
        attestation_url(normalized),
        headers={
            "User-Agent": "nexus7-railway-ci-attestation-gate/1.0",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urlopen(req, timeout=10) as response:  # nosec B310: fixed HTTPS host
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status != 200:
                return "pending", f"attestation HTTP {status}"
            body = response.read().decode("utf-8").strip().lower()
    except HTTPError as exc:
        if exc.code == 404:
            return "pending", "exact-SHA attestation not published yet"
        raise

    if body != normalized:
        return "failed", "exact-SHA attestation content mismatch"
    return "success", "exact-SHA Quality Check attestation present"


def wait_for_quality_check(
    sha: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
) -> tuple[bool, str]:
    """Wait fail-closed for the exact commit's post-success attestation."""
    normalized = validate_sha(sha)
    deadline = time.monotonic() + timeout_seconds
    last_detail = "not checked"

    while True:
        try:
            state, detail = fetch_attestation(normalized)
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError) as exc:
            state = "pending"
            detail = f"attestation source unavailable: {type(exc).__name__}: {exc}"

        last_detail = detail
        print(
            f"[CI_DEPLOY_GATE] sha={normalized[:12]} state={state} "
            f"source=exact_sha_attestation detail={detail}",
            flush=True,
        )

        if state == "success":
            return True, detail
        if state == "failed":
            return False, detail

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, f"timeout waiting for Quality Check attestation: {last_detail}"
        time.sleep(min(max(1, poll_seconds), remaining))


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
