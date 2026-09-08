"""Thin Python startup entrypoint for NEXUS-7 runtime hardenings.

Installation ordering and compatibility overlays live in bot.runtime_bootstrap.
This file intentionally owns only startup status and fail-closed diagnostics.
"""
import builtins
import sys

builtins._nexus_sitecustomize_status = "installing"

try:
    from bot.runtime_bootstrap import install as _install_runtime_bootstrap

    _install_runtime_bootstrap()
    builtins._nexus_sitecustomize_status = "ok"
except Exception as _sitecustomize_error:
    builtins._nexus_sitecustomize_status = "failed"
    print(
        "CRITICAL: NEXUS sitecustomize hardening installation failed: "
        f"{type(_sitecustomize_error).__name__}: {_sitecustomize_error}",
        file=sys.stderr,
    )
