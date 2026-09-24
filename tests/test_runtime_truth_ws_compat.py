from __future__ import annotations

import asyncio

from bot import runtime_truth_ws_compat


def test_runtime_truth_ws_compat_is_disabled_by_default(monkeypatch):
    monkeypatch.setenv("BGX_RUNTIME_TRUTH_ENABLED", "false")
    assert runtime_truth_ws_compat.install() is False


def test_runtime_truth_ws_compat_adds_anext_and_delegates_recv(monkeypatch):
    from websockets.legacy.protocol import WebSocketCommonProtocol

    monkeypatch.setenv("BGX_RUNTIME_TRUTH_ENABLED", "true")
    had_anext = hasattr(WebSocketCommonProtocol, "__anext__")
    previous = getattr(WebSocketCommonProtocol, "__anext__", None)
    if had_anext:
        delattr(WebSocketCommonProtocol, "__anext__")

    class FakeProtocol:
        async def recv(self):
            return "frame"

    try:
        assert runtime_truth_ws_compat.install() is True
        method = WebSocketCommonProtocol.__anext__
        assert asyncio.run(method(FakeProtocol())) == "frame"
        assert runtime_truth_ws_compat.install() is False
    finally:
        if hasattr(WebSocketCommonProtocol, "__anext__"):
            delattr(WebSocketCommonProtocol, "__anext__")
        if had_anext:
            setattr(WebSocketCommonProtocol, "__anext__", previous)
