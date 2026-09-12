"""Minimal fail-closed startup entrypoint for BGX runtime hardenings.

Installation ordering lives in ``bot.runtime_bootstrap`` so the runtime stack is
explicit and testable while hardenings are progressively migrated into core.

``main_hardened.py`` is also a real production entrypoint and imports
``bot.service_readiness`` for Railway's infrastructure readiness endpoint. This
reference is documented here because the self-check scans root startup
entrypoints, including this module, when determining whether a bot module is
actually orphaned. Keeping the production entrypoint dependency visible here
prevents a false dead-code warning without importing or executing readiness
logic during interpreter startup.
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
        "CRITICAL: BGX sitecustomize hardening installation failed: "
        f"{type(_sitecustomize_error).__name__}: {_sitecustomize_error}",
        file=sys.stderr,
    )
