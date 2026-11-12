"""SidecarStreamProvider.finish 失败/成功后的资源收束（读线程、HTTP 事件流、本地映射）。

确定性假 client（无网络）覆盖：HTTP 503、事件流提前断开、finish 非 LogicError、重复 cancel、
cancel 非 404 失败不被吞、成功路径一个 final。另一条走真实 loopback HTTP FakeEngine sidecar
→ 应用 session：定稿失败 503 → 应用 error/closed、不入队、无残留线程、close 不再对旧会话发 cancel。
不启 GPU，不用 17945/17946/11434。
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import httpx
import pytest

from _asr_provider import SidecarStreamProvider
from _contracts import (
    ErrorCode,
    LogicError,
    RecognizeSessionCreate,
    RecognizeSessionCreated,
    SessionClosedEvent,
    SessionClosedReason,
    SessionErrorEvent,
    SessionFinalEvent,
    SessionPartialEvent,
)
from tests.integration.test_sidecar_provider_partials import (
    _app_headers,
    _create_session,
    _leftover_reader_threads,
    _post_chunk,
    _post_finish,
    _submit,
    fake_sidecar,
    isolated_sidecar_app,
    start_app_sse,
    stop_app_sse,
)
from tests.sidecar.fake_engine import FakeEngine

_END = object()


class _FakeResponse:
    def __init__(self, feed: "queue.Queue[object]") -> None:
        self.closed = threading.Event()
        self._feed = feed

    def close(self) -> None:
        self.closed.set()
        self._feed.put(_END)


class _FakeClient:
    """只实现 provider 用到的 AsrSidecarClient 方法；事件由测试逐个投喂。"""

    def __init__(self) -> None:
        self.feed: "queue.Queue[object]" = queue.Queue()
        self.responses: list[_FakeResponse] = []
        self.finish_result: SessionFinalEvent | BaseException | None = None
        self.cancel_error: BaseException | None = None
        self.cancel_calls: list[str] = []
        self.finish_calls: list[str] = []

    def create_session(self, body: RecognizeSessionCreate | None = None) -> RecognizeSessionCreated:
        return RecognizeSessionCreated(session_id="remote-1")

    def iter_events(self, session_id: str, *, attach_response=None):
        response = _FakeResponse(self.feed)
        self.responses.append(response)
        if attach_response is not None:
            attach_response(response)
        while True:
            item = self.feed.get(timeout=10)
            if item is _END:
                if response.closed.is_set():
                    raise httpx.ReadError("stream closed")
                return
            yield item

    def send_chunk(self, session_id: str, index: int, pcm: bytes) -> None:
        return None

    def finish(self, session_id: str):
        self.finish_calls.append(session_id)
        result = self.finish_result
        if isinstance(result, BaseException):
            raise result
        return result

    def cancel(self, session_id: str) -> None:
        self.cancel_calls.append(session_id)
        if self.cancel_error is not None:
            raise self.cancel_error


class _Sink:
    def __init__(self) -> None:
        self.partials: list[SessionPartialEvent] = []
        self.errors: list[SessionErrorEvent] = []

    def publish_partial(self, session_id: str, event: SessionPartialEvent) -> None:
        self.partials.append(event)

    def publish_provider_error(self, session_id: str, event: SessionErrorEvent) -> None:
        self.errors.append(event)


def _started(client: _FakeClient) -> tuple[SidecarStreamProvider, _Sink]:
    provider = SidecarStreamProvider(client)  # type: ignore[arg-type]
    sink = _Sink()
    provider.start("app-1", RecognizeSessionCreate(), sink=sink)  # type: ignore[arg-type]
    return provider, sink


def _assert_released(provider: SidecarStreamProvider, client: _FakeClient) -> None:
    assert provider._map == {} and provider._bridges == {}
    assert provider.leftover_threads() == []
    assert not [t.name for t in threading.enumerate() if t.name.startswith("sidecar-sse-app-1")]


def _partial(text: str) -> SessionPartialEvent:
    return SessionPartialEvent(session_id="remote-1", stable_text="", tail_text=text, language="zh", audio_end_s=0.5)


def test_finish_http_503_after_sse_error_releases_everything_and_keeps_failure():
    client = _FakeClient()
    provider, sink = _started(client)
    client.feed.put(_partial("前文"))
    client.feed.put(SessionErrorEvent(session_id="remote-1", error=ErrorCode.asr_unavailable, message="定稿失控"))
    client.feed.put(SessionClosedEvent(session_id="remote-1", reason=SessionClosedReason.error))
    client.finish_result = LogicError(ErrorCode.asr_unavailable, "ASR sidecar 返回 HTTP 503")
    client.cancel_error = LogicError(ErrorCode.not_found, "会话不存在")
    with pytest.raises(LogicError) as caught:
        provider.finish("app-1")
    assert caught.value.code == ErrorCode.asr_unavailable
    assert any("not_found" in note for note in getattr(caught.value, "__notes__", []))
    assert [e.error for e in sink.errors][:1] == [ErrorCode.asr_unavailable]
    assert client.cancel_calls == ["remote-1"]
    _assert_released(provider, client)
    # 失败后重复 cancel / close 都是无副作用的空操作，不再对旧会话发请求
    provider.cancel("app-1")
    provider.cancel("app-1")
    provider.close()
    assert client.cancel_calls == ["remote-1"]


def test_finish_http_503_without_any_sse_event_still_publishes_error_and_closes_stream():
    client = _FakeClient()
    provider, sink = _started(client)
    client.finish_result = LogicError(ErrorCode.asr_unavailable, "ASR sidecar 返回 HTTP 503")
    client.cancel_error = LogicError(ErrorCode.not_found, "会话不存在")
    t0 = time.monotonic()
    with pytest.raises(LogicError):
        provider.finish("app-1")
    assert time.monotonic() - t0 < 3.0, "失败路径不得卡在 settle 超时上"
    assert [e.error for e in sink.errors] == [ErrorCode.asr_unavailable]
    assert client.responses and client.responses[0].closed.is_set(), "HTTP 事件流必须被关闭"
    _assert_released(provider, client)


def test_early_sse_disconnect_then_finish_is_error_not_final():
    client = _FakeClient()
    provider, sink = _started(client)
    client.feed.put(_partial("半句"))
    client.feed.put(_END)  # 事件流在 closed 前结束
    deadline = time.monotonic() + 3
    while provider.leftover_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    client.finish_result = SessionFinalEvent(session_id="remote-1", text="不该被采纳", language="zh")
    with pytest.raises(LogicError) as caught:
        provider.finish("app-1")
    assert caught.value.code == ErrorCode.asr_unavailable
    assert sink.errors and sink.errors[0].error == ErrorCode.asr_unavailable
    _assert_released(provider, client)


def test_finish_transport_error_is_wrapped_visible_and_released():
    client = _FakeClient()
    provider, sink = _started(client)
    client.finish_result = httpx.ConnectError("refused")
    with pytest.raises(httpx.ConnectError):
        provider.finish("app-1")
    assert [e.error for e in sink.errors] == [ErrorCode.asr_unavailable]
    assert client.cancel_calls == ["remote-1"]
    _assert_released(provider, client)


def test_finish_failure_cancel_non_404_is_not_swallowed():
    client = _FakeClient()
    provider, _sink = _started(client)
    client.finish_result = LogicError(ErrorCode.asr_unavailable, "ASR sidecar 返回 HTTP 503")
    client.cancel_error = LogicError(ErrorCode.internal, "sidecar 内部错误")
    with pytest.raises(LogicError) as caught:
        provider.finish("app-1")
    assert caught.value.code == ErrorCode.asr_unavailable
    assert any("internal" in note for note in caught.value.__notes__)
    _assert_released(provider, client)


def test_finish_unknown_session_404_is_raised_not_swallowed():
    client = _FakeClient()
    provider, sink = _started(client)
    client.finish_result = LogicError(ErrorCode.not_found, "会话不存在")
    client.cancel_error = LogicError(ErrorCode.not_found, "会话不存在")
    with pytest.raises(LogicError) as caught:
        provider.finish("app-1")
    assert caught.value.code == ErrorCode.not_found
    assert [e.error for e in sink.errors] == [ErrorCode.not_found]
    _assert_released(provider, client)


def test_success_path_one_final_and_released():
    client = _FakeClient()
    provider, sink = _started(client)
    client.feed.put(_partial("你好"))
    client.feed.put(SessionFinalEvent(session_id="remote-1", text="你好。", language="zh"))
    client.feed.put(SessionClosedEvent(session_id="remote-1", reason=SessionClosedReason.finished))
    client.finish_result = SessionFinalEvent(session_id="remote-1", text="你好。", language="zh")
    final = provider.finish("app-1")
    assert (final.session_id, final.text, final.language) == ("app-1", "你好。", "zh")
    assert sink.errors == [] and len(sink.partials) == 1
    assert client.cancel_calls == []
    _assert_released(provider, client)
    with pytest.raises(KeyError):
        provider.finish("app-1")


class _FailingFinishEngine(FakeEngine):
    def finish_stream(self, session_id: str) -> SessionFinalEvent:
        self.streams.pop(session_id, None)
        raise LogicError(ErrorCode.asr_unavailable, "定稿解码超过语速上界（模型复读失控），拒绝把截断结果当定稿")


def test_http_sidecar_finish_503_closes_app_session_without_job_or_leftovers(tmp_path: Path):
    with fake_sidecar(engine=_FailingFinishEngine(text="loop")) as (_engine, _server, client, provider):
        with isolated_sidecar_app(tmp_path, provider) as comp:
            headers = _app_headers(comp)
            sid = _create_session(comp, headers)
            pump = start_app_sse(
                comp.app,
                path=f"/api/v1/recognize/sessions/{sid}/events",
                headers=headers,
                bind=(comp.bind_host, comp.bind_port),
            )
            try:
                assert _post_chunk(comp, headers, sid)["status"] == 202
                pump.wait_partials(1)
                finish = _post_finish(comp, headers, sid)
                assert finish["status"] == 503, finish
                pump.wait_closed(timeout=8.0)
                kinds = pump.kinds()
                assert "final" not in kinds
                assert kinds.count("error") == 1 and kinds[-1] == "closed", kinds
                closed = next(item for item in pump.snapshot() if item.get("event") == "closed")
                assert closed["reason"] == "error"
                submitted = _submit(comp, headers, sid, "job-after-503")
                assert submitted["status"] == 400, submitted
                assert list(comp.runtime.jobs._store.list_jobs()) == []
                assert provider._map == {} and provider._bridges == {}
                assert provider.leftover_threads() == []
                again = _create_session(comp, headers)
                assert again != sid
            finally:
                stop_app_sse(pump)
    assert not _leftover_reader_threads()
