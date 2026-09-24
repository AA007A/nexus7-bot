from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from bot import runtime_truth_ws_compat


def test_runtime_truth_ws_compat_is_disabled_by_default():
    with patch.dict(os.environ, {"BGX_RUNTIME_TRUTH_ENABLED": "false"}, clear=False):
        assert runtime_truth_ws_compat.install() is False


def test_runtime_truth_ws_compat_adds_anext_and_delegates_recv():
    from websockets.legacy.protocol import WebSocketCommonProtocol

    had_anext = hasattr(WebSocketCommonProtocol, "__anext__")
    previous_anext = getattr(WebSocketCommonProtocol, "__anext__", None)
    previous_recv = WebSocketCommonProtocol.recv
    if had_anext:
        delattr(WebSocketCommonProtocol, "__anext__")

    async def fake_recv(self):
        return "frame"

    WebSocketCommonProtocol.recv = fake_recv
    protocol = object.__new__(WebSocketCommonProtocol)

    try:
        with patch.dict(os.environ, {"BGX_RUNTIME_TRUTH_ENABLED": "true"}, clear=False):
            assert runtime_truth_ws_compat.install() is True
            method = WebSocketCommonProtocol.__anext__
            assert asyncio.run(method(protocol)) == "frame"
            assert runtime_truth_ws_compat.install() is False
    finally:
        WebSocketCommonProtocol.recv = previous_recv
        if hasattr(WebSocketCommonProtocol, "__anext__"):
            delattr(WebSocketCommonProtocol, "__anext__")
        if had_anext:
            setattr(WebSocketCommonProtocol, "__anext__", previous_anext)
