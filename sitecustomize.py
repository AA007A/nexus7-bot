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
    # Install logging-only semantic normalization before bootstrap so even
    # legacy startup wrappers cannot emit contradictory sizing authority text.
    from bot.sizing_semantics_log_hardening import install as _install_sizing_semantics
    _install_sizing_semantics()

    # Runtime-truth only: websockets==12.0 protocols are async iterables but
    # don't expose __anext__ directly. The truth WS proxy delegates __anext__
    # to the wrapped protocol, so install the compatibility shim before the
    # runtime bootstrap creates any telemetry-enabled transport wrappers.
    # The shim is a strict no-op unless BGX_RUNTIME_TRUTH_ENABLED=true.
    from bot.runtime_truth_ws_compat import install as _install_runtime_truth_ws_compat
    _install_runtime_truth_ws_compat()

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
