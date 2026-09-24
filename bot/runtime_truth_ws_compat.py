"""Compatibility shim for runtime-truth WebSocket observation.

The production dependency ``websockets==12.0`` exposes its legacy protocol as
an async iterable whose ``__aiter__`` returns an async generator; the protocol
object itself doesn't provide ``__anext__``. The runtime-truth WS proxy delegates
``__anext__`` to the wrapped protocol, so telemetry-enabled production needs a
minimal compatibility method that preserves the protocol's normal async-for
semantics.

This module is observability-only. It is a no-op unless
``BGX_RUNTIME_TRUTH_ENABLED=true`` and it doesn't import or call any trading or
exchange mutation code.
"""
from __future__ import annotations

import os


def _enabled() -> bool:
    return os.environ.get("BGX_RUNTIME_TRUTH_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def install() -> bool:
    """Install ``__anext__`` on the legacy websocket protocol when required.

    Returns True only when this call adds the compatibility method. Existing
    implementations are left untouched. Normal connection closure maps to
    ``StopAsyncIteration`` exactly as ``async for`` expects; abnormal closure
    exceptions continue to propagate to the existing KuCoin reconnect loop.
    """
    if not _enabled():
        return False

    try:
        from websockets.exceptions import ConnectionClosedOK
        from websockets.legacy.protocol import WebSocketCommonProtocol
    except Exception:
        return False

    if hasattr(WebSocketCommonProtocol, "__anext__"):
        return False

    async def _runtime_truth_anext(protocol):
        try:
            return await WebSocketCommonProtocol.recv(protocol)
        except ConnectionClosedOK as exc:
            raise StopAsyncIteration from exc

    setattr(WebSocketCommonProtocol, "__anext__", _runtime_truth_anext)
    return True
