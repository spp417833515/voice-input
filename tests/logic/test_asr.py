from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from _asr import AsrSidecarClient
from _contracts import (
    ASR_STREAMING_BLOCKERS,
    SIDECAR_CHUNK_HEADER,
    ErrorCode,
    LogicError,
    RecognizeSessionCreate,
    SessionClosedEvent,
    SessionClosedReason,
    SessionFinalEvent,
    SessionPartialEvent,
)

ROOT = Path(__file__).resolve().parents[2]


def test_source_does_not_import_forbidden_runtimes():
    tree = ast.parse((ROOT / "_asr.py").read_text(encoding="utf-8"))
    forbidden = {"torch", "vllm", "faster_whisper", "whisper"}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in forbidden:
                    found.add(top)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top in forbidden:
                found.add(top)
    assert not found


def test_blockers_exposed_on_client():
    client = AsrSidecarClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert client.blockers == ASR_STREAMING_BLOCKERS
    client.close()


def test_transcribe_and_session_flow_with_mock_sse():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v1/transcribe"):
            return httpx.Response(200, json={"ok": True, "text": "甚至出现交易几乎停滞的情况。", "language": "zh"})
        if path.endswith("/v1/sessions") and request.method == "POST":
            return httpx.Response(200, json={"session_id": "sess-1"})
        if path.endswith("/chunks"):
            assert request.headers.get(SIDECAR_CHUNK_HEADER) == "0"
            return httpx.Response(200, json={"accepted": True, "index": 0})
        if path.endswith("/events"):
            body = (
                "event: partial\n"
                "data: {\"event\":\"partial\",\"session_id\":\"sess-1\",\"stable_text\":\"\",\"tail_text\":\"甚至\"}\n"
                "\n"
                "event: final\n"
                "data: {\"event\":\"final\",\"session_id\":\"sess-1\",\"text\":\"甚至出现交易几乎停滞的情况。\",\"language\":\"Chinese\"}\n"
                "\n"
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        if path.endswith("/finish"):
            return httpx.Response(
                200,
                json={"event": "final", "session_id": "sess-1", "text": "甚至出现交易几乎停滞的情况。"},
            )
        if path.endswith("/cancel"):
            return httpx.Response(
                200,
                json={"event": "closed", "session_id": "sess-1", "reason": "cancelled"},
            )
        return httpx.Response(404, text="no")

    client = AsrSidecarClient(transport=httpx.MockTransport(handler), timeout_s=5)
    stt = client.transcribe(b"RIFF", language="zh")
    assert stt.text.startswith("甚至")
    created = client.create_session(RecognizeSessionCreate(language="zh"))
    assert created.session_id == "sess-1"
    acc = client.send_chunk("sess-1", 0, b"\x00\x00")
    assert acc.accepted is True
    events = list(client.iter_events("sess-1"))
    assert isinstance(events[0], SessionPartialEvent)
    assert events[0].stable_text == ""
    assert events[0].tail_text == "甚至"
    assert isinstance(events[1], SessionFinalEvent)
    finished = client.finish("sess-1")
    assert isinstance(finished, SessionFinalEvent)
    cancelled = client.cancel("sess-1")
    assert isinstance(cancelled, SessionClosedEvent)
    assert cancelled.reason == SessionClosedReason.cancelled
    client.close()


def test_timeout_conflict_and_bad_sse():
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("late")

    client = AsrSidecarClient(transport=httpx.MockTransport(timeout), timeout_s=0.01)
    with pytest.raises(LogicError) as ei:
        client.transcribe(b"x")
    assert ei.value.code == ErrorCode.timeout
    client.close()

    def conflict(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="dup")

    client = AsrSidecarClient(transport=httpx.MockTransport(conflict))
    with pytest.raises(LogicError) as ei:
        client.send_chunk("s", 1, b"x")
    assert ei.value.code == ErrorCode.conflict
    client.close()

    def bad_sse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: not-json\n\n")

    client = AsrSidecarClient(transport=httpx.MockTransport(bad_sse))
    with pytest.raises(LogicError) as ei:
        list(client.iter_events("s"))
    assert "JSON" in ei.value.message
    client.close()


def test_cancel_must_not_look_like_final():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"event": "closed", "session_id": "s", "reason": "finished"},
        )

    client = AsrSidecarClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LogicError) as ei:
        client.cancel("s")
    assert "cancelled" in ei.value.message
    client.close()
