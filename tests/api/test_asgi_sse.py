"""原始 ASGI 有限 SSE 发送 / 断连，以及有界队列与可注入时钟的清理。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from _contracts import ProductBusyEvent, RecognizeSessionCreate, SessionClosedReason
from _v3.sessions import RecognizeSessionHub
from _v3_app import build_sandbox_runtime, create_app
from tests.api.asgi_client import call_asgi, collect_sse
from tests.api.conftest import TEST_TOKEN
from tests.api.providers import FixtureAsrProvider, fixture_transcribe


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


LOCAL_HEADERS = {"host": "127.0.0.1:17945", "origin": "https://127.0.0.1:17945"}
LOCAL = ("127.0.0.1", 50000)


@pytest.fixture
def sse_app(tmp_path: Path):
    clock = FakeClock()
    runtime = build_sandbox_runtime(
        root=tmp_path / "sandbox",
        token=TEST_TOKEN,
        transcribe=fixture_transcribe,
        stream_provider=FixtureAsrProvider(final_text="fixture-final"),
        sse_queue_max=2,
        session_retain_closed_s=10.0,
        session_idle_open_s=20.0,
        clock=clock,
    )
    app = create_app(runtime)
    return app, runtime, clock


def test_product_sse_asgi_sends_idle_and_cleans_on_disconnect(sse_app):
    app, runtime, _clock = sse_app
    result = asyncio.run(
        collect_sse(
            app,
            path="/api/v1/events",
            client=LOCAL,
            headers=LOCAL_HEADERS,
            disconnect_after_events=1,
            timeout=2.0,
        )
    )
    assert result["status"] == 200, result
    assert "text/event-stream" in result["headers"].get("content-type", "")
    assert result["events"][0]["event"] == "idle"
    assert runtime.events._listeners == []


def test_session_sse_asgi_emits_final_then_closed(sse_app):
    app, _runtime, _clock = sse_app
    created = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path="/api/v1/recognize/sessions",
            client=LOCAL,
            headers={**LOCAL_HEADERS, "content-type": "application/json"},
            body=b'{"language":"zh"}',
        )
    )
    sid = created["body"]["session_id"]
    hub: RecognizeSessionHub = app.state.sessions

    def _finish():
        hub.finish(sid)

    result = asyncio.run(
        collect_sse(
            app,
            path=f"/api/v1/recognize/sessions/{sid}/events",
            client=LOCAL,
            headers=LOCAL_HEADERS,
            after_start=_finish,
            timeout=2.0,
        )
    )
    assert result["status"] == 200, result
    names = [item.get("event") for item in result["events"]]
    assert names[:2] == ["final", "closed"] or names == ["final", "closed"], result["events"]
    assert result["events"][0]["text"] == "fixture-final"
    assert result["events"][1]["reason"] == "finished"
    assert hub._sessions[sid].subscribers == []


def test_session_sse_asgi_disconnect_before_finish(sse_app):
    app, runtime, _clock = sse_app
    created = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path="/api/v1/recognize/sessions",
            client=LOCAL,
            headers={**LOCAL_HEADERS, "content-type": "application/json"},
            body=b'{"language":"zh"}',
        )
    )
    sid = created["body"]["session_id"]
    result = asyncio.run(
        collect_sse(
            app,
            path=f"/api/v1/recognize/sessions/{sid}/events",
            client=LOCAL,
            headers=LOCAL_HEADERS,
            disconnect_after_events=0,
            timeout=1.0,
        )
    )
    # 主动 disconnect（尚未收到事件）后会话应记为 disconnected
    snap = runtime.finals.lookup(sid)
    assert snap is not None
    assert snap.closed_reason == SessionClosedReason.disconnected
    finish = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/finish",
            client=LOCAL,
            headers={**LOCAL_HEADERS, "content-type": "application/json"},
            body=b"{}",
        )
    )
    assert finish["status"] == 409, finish
    assert app.state.sessions._sessions[sid].subscribers == []
    assert result["status"] in {200, None} or result["status"] == 200


def test_product_sse_overflow_visibly_terminates(sse_app):
    app, runtime, _clock = sse_app

    def flood():
        for _ in range(8):
            runtime.events.publish(ProductBusyEvent())

    result = asyncio.run(
        collect_sse(
            app,
            path="/api/v1/events",
            client=LOCAL,
            headers=LOCAL_HEADERS,
            after_idle=flood,
            timeout=2.0,
        )
    )
    assert result["status"] == 200, result
    names = [item.get("event") for item in result["events"]]
    assert "idle" in names
    assert "error" in names
    err = next(item for item in result["events"] if item.get("event") == "error")
    assert "队列已满" in err["message"]
    assert runtime.events._listeners == []


def test_session_eviction_and_idle_timeout_use_injected_clock(sse_app):
    app, runtime, clock = sse_app
    hub: RecognizeSessionHub = app.state.sessions
    created = hub.create(RecognizeSessionCreate(language="zh"))
    sid = created.session_id
    assert hub.has_session(sid)
    clock.advance(20.0)
    swept = hub.sweep()
    assert sid in swept["idle_disconnected"]
    assert runtime.finals.lookup(sid) is not None
    assert runtime.finals.lookup(sid).closed_reason == SessionClosedReason.disconnected
    clock.advance(10.0)
    evicted = hub.sweep()
    assert sid in evicted["evicted"]
    assert not hub.has_session(sid)
    # 状态仍可从 finals 查询，不因驱逐丢失
    assert runtime.finals.lookup(sid).closed_reason == SessionClosedReason.disconnected


def test_finished_session_retained_until_clock_advances(sse_app):
    app, _runtime, clock = sse_app
    hub: RecognizeSessionHub = app.state.sessions
    created = hub.create(RecognizeSessionCreate(language="zh"))
    sid = created.session_id
    hub.finish(sid)
    assert hub.has_session(sid)
    clock.advance(9.0)
    assert sid not in hub.sweep()["evicted"]
    assert hub.has_session(sid)
    clock.advance(1.0)
    assert sid in hub.sweep()["evicted"]
    assert not hub.has_session(sid)
