from __future__ import annotations

import json
from unittest.mock import patch

from bot import runtime_truth, runtime_truth_ws_filter


def _frame(topic=None, message_type="message", data=None):
    payload = {"type": message_type}
    if topic is not None:
        payload["topic"] = topic
    if data is not None:
        payload["data"] = data
    return json.dumps(payload, separators=(",", ":"))


def test_runtime_truth_ws_filter_accepts_exact_kline_shape():
    payload = _frame(
        "/contractMarket/limitCandle:XBTUSDTM_15min",
        data={"candles": [1790211000, "1", "2", "3", "0.5", "7", "11"]},
    )
    assert runtime_truth_ws_filter.replay_relevant(payload) is True
    assert runtime_truth_ws_filter.replay_relevant(payload.encode("utf-8")) is True


def test_runtime_truth_ws_filter_rejects_high_frequency_ticker_and_pong():
    ticker = _frame(
        "/contractMarket/tickerV2:XBTUSDTM,ETHUSDTM",
        data={"price": "1"},
    )
    pong = _frame(message_type="pong")
    assert runtime_truth_ws_filter.replay_relevant(ticker) is False
    assert runtime_truth_ws_filter.replay_relevant(pong) is False


def test_runtime_truth_ws_filter_keeps_transport_control_evidence():
    assert runtime_truth_ws_filter.replay_relevant(_frame(message_type="ack")) is True
    assert runtime_truth_ws_filter.replay_relevant(_frame(message_type="error")) is True
    assert runtime_truth_ws_filter.replay_relevant(_frame(message_type="welcome")) is True


def test_runtime_truth_ws_filter_rejects_malformed_and_non_kline_messages():
    assert runtime_truth_ws_filter.replay_relevant("not-json") is False
    assert runtime_truth_ws_filter.replay_relevant("[]") is False
    assert runtime_truth_ws_filter.replay_relevant(_frame("/contractMarket/limitCandle:XBTUSDTM_15min", data={})) is False


def test_runtime_truth_ws_filter_does_not_allocate_sequence_for_filtered_ticker():
    original_enabled = runtime_truth._ENABLED
    original_recorder_enabled = runtime_truth.RECORDER.enabled
    original_capture = runtime_truth.capture_ws_application_payload
    had_marker = hasattr(runtime_truth, "_ws_replay_filter_installed")
    previous_marker = getattr(runtime_truth, "_ws_replay_filter_installed", None)
    try:
        runtime_truth._ENABLED = True
        runtime_truth.RECORDER.enabled = True
        runtime_truth.capture_ws_application_payload = original_capture
        if hasattr(runtime_truth, "_ws_replay_filter_installed"):
            delattr(runtime_truth, "_ws_replay_filter_installed")
        assert runtime_truth_ws_filter.install() is True
        before = runtime_truth.RECORDER.current_sequence
        ticker = _frame("/contractMarket/tickerV2:XBTUSDTM", data={"price": "1"})
        assert runtime_truth.capture_ws_application_payload(ticker, "test-session") is None
        assert runtime_truth.RECORDER.current_sequence == before
    finally:
        runtime_truth.capture_ws_application_payload = original_capture
        runtime_truth._ENABLED = original_enabled
        runtime_truth.RECORDER.enabled = original_recorder_enabled
        if hasattr(runtime_truth, "_ws_replay_filter_installed"):
            delattr(runtime_truth, "_ws_replay_filter_installed")
        if had_marker:
            runtime_truth._ws_replay_filter_installed = previous_marker


def test_runtime_truth_ws_filter_preserves_kline_payload_for_original_capture():
    calls = []
    original_enabled = runtime_truth._ENABLED
    original_capture = runtime_truth.capture_ws_application_payload
    had_marker = hasattr(runtime_truth, "_ws_replay_filter_installed")
    previous_marker = getattr(runtime_truth, "_ws_replay_filter_installed", None)

    def fake_original(payload, session_id):
        calls.append((payload, session_id))
        return "event-id"

    payload = _frame(
        "/contractMarket/limitCandle:ETHUSDTM_1hour",
        data={"candles": [1790211000, "1", "2", "3", "0.5", "7", "11"]},
    )
    try:
        runtime_truth._ENABLED = True
        runtime_truth.capture_ws_application_payload = fake_original
        if hasattr(runtime_truth, "_ws_replay_filter_installed"):
            delattr(runtime_truth, "_ws_replay_filter_installed")
        assert runtime_truth_ws_filter.install() is True
        assert runtime_truth.capture_ws_application_payload(payload, "session-a") == "event-id"
        assert calls == [(payload, "session-a")]
    finally:
        runtime_truth.capture_ws_application_payload = original_capture
        runtime_truth._ENABLED = original_enabled
        if hasattr(runtime_truth, "_ws_replay_filter_installed"):
            delattr(runtime_truth, "_ws_replay_filter_installed")
        if had_marker:
            runtime_truth._ws_replay_filter_installed = previous_marker


def test_runtime_truth_ws_filter_install_is_noop_when_truth_disabled():
    with patch.object(runtime_truth, "_ENABLED", False):
        assert runtime_truth_ws_filter.install() is False
