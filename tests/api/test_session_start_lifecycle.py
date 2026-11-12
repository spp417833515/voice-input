"""B2：本地会话身份先于外部 provider.start；启动失败不得 200，不得留下 open 僵尸。"""
from __future__ import annotations

import threading

import pytest

from _contracts import (
    ErrorCode,
    LogicError,
    RecognizeSessionCreate,
    SessionErrorEvent,
    SessionFinalEvent,
    SessionPartialEvent,
)
from _session_finals import SessionFinalIndex
from _v3.sessions import RecognizeSessionHub, session_replay_source


class ScriptedProvider:
    official_engine = False

    def __init__(
        self,
        *,
        sync_partials: list[str] | None = None,
        sync_error: str | None = None,
        raise_after: BaseException | None = None,
        concurrent_partial: str | None = None,
        block: bool = False,
    ) -> None:
        self.sync_partials = list(sync_partials or [])
        self.sync_error = sync_error
        self.raise_after = raise_after
        self.concurrent_partial = concurrent_partial
        self.block = block
        self.started: dict[str, RecognizeSessionCreate] = {}
        self.cancelled: list[str] = []
        self.finished: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def start(self, session_id: str, body: RecognizeSessionCreate, *, sink=None) -> None:
        self.started[session_id] = body
        self.entered.set()
        if self.concurrent_partial and sink is not None:
            done = threading.Event()

            def _emit() -> None:
                sink.publish_partial(
                    session_id,
                    SessionPartialEvent(
                        session_id=session_id,
                        stable_text="",
                        tail_text=self.concurrent_partial,
                        language="zh",
                    ),
                )
                done.set()

            worker = threading.Thread(target=_emit, name="scripted-early-partial", daemon=True)
            worker.start()
            assert done.wait(timeout=2.0), "并发 early partial 未在 start 返回前投出"
            worker.join(timeout=2.0)
            assert not worker.is_alive()
        for tail in self.sync_partials:
            if sink is not None:
                sink.publish_partial(
                    session_id,
                    SessionPartialEvent(session_id=session_id, stable_text="", tail_text=tail, language="zh"),
                )
        if self.sync_error and sink is not None:
            sink.publish_provider_error(
                session_id,
                SessionErrorEvent(session_id=session_id, error=ErrorCode.asr_unavailable, message=self.sync_error),
            )
        if self.block:
            if not self.release.wait(timeout=5.0):
                raise AssertionError("测试未释放阻塞的 start")
        if self.raise_after is not None:
            raise self.raise_after

    def push_pcm(self, session_id: str, index: int, pcm: bytes) -> None:
        del session_id, index, pcm
        return None

    def finish(self, session_id: str) -> SessionFinalEvent:
        self.finished.append(session_id)
        return SessionFinalEvent(session_id=session_id, text="scripted-final", language="zh")

    def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)
        self.release.set()


def _hub(provider) -> RecognizeSessionHub:
    return RecognizeSessionHub(
        finals=SessionFinalIndex(),
        provider=provider,
        max_chunk_bytes=64_000,
        max_session_pcm_bytes=160_000,
    )


def test_sync_early_partial_is_kept_and_replayed_before_finish():
    provider = ScriptedProvider(sync_partials=["early-sync-tail"])
    hub = _hub(provider)
    created = hub.create(RecognizeSessionCreate(language="zh"))
    sid = created.session_id
    session = hub._sessions[sid]
    assert session.state == "open"
    assert session.provider_ready is True
    assert session.latest_partial is not None
    assert session.latest_partial.tail_text == "early-sync-tail"
    assert session.latest_partial.stable_text == ""
    assert [ev.event for ev in session_replay_source(session)] == ["partial"]
    sub = hub.subscribe(sid)
    first = sub.get(timeout=1.0)
    assert first.event == "partial"
    assert first.tail_text == "early-sync-tail"
    closed = hub.finish(sid)
    assert closed.reason.value == "finished"
    kinds = []
    while not sub.empty():
        kinds.append(sub.get_nowait().event)
    assert kinds == ["final", "closed"]
    assert kinds.count("final") == 1
    hub.unsubscribe(sid, sub)
    assert session.state == "finished"
    assert not any(t.name.startswith("scripted-early-partial") for t in threading.enumerate())


def test_concurrent_early_partial_before_start_returns():
    provider = ScriptedProvider(concurrent_partial="early-concurrent-tail")
    hub = _hub(provider)
    created = hub.create(RecognizeSessionCreate(language="zh"))
    sid = created.session_id
    assert hub._sessions[sid].latest_partial is not None
    assert hub._sessions[sid].latest_partial.tail_text == "early-concurrent-tail"
    sub = hub.subscribe(sid)
    ev = sub.get(timeout=1.0)
    assert ev.event == "partial" and ev.tail_text == "early-concurrent-tail"
    hub.unsubscribe(sid, sub)
    hub.cancel(sid)
    assert hub._sessions[sid].state == "cancelled"


def test_sync_error_during_start_does_not_return_created_or_leave_open():
    provider = ScriptedProvider(sync_error="start 内同步错误")
    hub = _hub(provider)
    with pytest.raises(LogicError) as caught:
        hub.create(RecognizeSessionCreate(language="zh"))
    assert caught.value.code in {ErrorCode.conflict, ErrorCode.asr_unavailable}
    assert len(provider.started) == 1
    sid = next(iter(provider.started))
    assert hub.has_session(sid)
    session = hub._sessions[sid]
    assert session.state == "error"
    assert session.state != "open"
    assert session.provider_ready is False
    assert session.closed is not None and session.closed.reason.value == "error"
    kinds = [ev.event for ev in session_replay_source(session)]
    assert "error" in kinds
    assert "final" not in kinds
    assert kinds.count("closed") == 1
    assert session.final is None
    assert session.error is not None
    with pytest.raises(LogicError) as finish_err:
        hub.finish(sid)
    assert finish_err.value.code == ErrorCode.conflict
    snap = hub._finals.lookup(sid)
    assert snap is not None
    assert snap.has_final is False
    assert snap.text == ""
    assert snap.closed_reason is not None
    assert snap.closed_reason.value == "error"
    assert sid in provider.cancelled or provider.cancelled


def test_start_raise_after_partial_rolls_back_and_is_not_created():
    provider = ScriptedProvider(
        sync_partials=["kept-then-fail"],
        raise_after=LogicError(ErrorCode.asr_unavailable, "sidecar 启动失败"),
    )
    hub = _hub(provider)
    with pytest.raises(LogicError) as caught:
        hub.create(RecognizeSessionCreate(language="zh"))
    assert "启动失败" in caught.value.message or caught.value.code == ErrorCode.asr_unavailable
    sid = next(iter(provider.started))
    session = hub._sessions[sid]
    assert session.state == "error"
    assert session.state != "open"
    assert session.latest_partial is not None
    assert session.latest_partial.tail_text == "kept-then-fail"
    assert session.error is not None
    assert session.final is None
    assert sid in provider.cancelled
    assert provider.finished == []


def test_cancel_during_start_does_not_return_created_or_leave_open():
    provider = ScriptedProvider(block=True)
    hub = _hub(provider)
    result: dict = {}

    def _cancel() -> None:
        assert provider.entered.wait(timeout=2.0)
        sids = [item.session_id for item in hub._sessions.values() if item.state == "open"]
        assert sids, "登记必须先于阻塞的 start"
        result["sid"] = sids[0]
        closed = hub.cancel(sids[0])
        result["reason"] = closed.reason.value
        provider.release.set()

    worker = threading.Thread(target=_cancel, name="cancel-during-start", daemon=True)
    worker.start()
    with pytest.raises(LogicError) as caught:
        hub.create(RecognizeSessionCreate(language="zh"))
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert caught.value.code == ErrorCode.conflict
    sid = result["sid"]
    assert result["reason"] == "cancelled"
    session = hub._sessions[sid]
    assert session.state == "cancelled"
    assert session.state != "open"
    assert session.final is None
    assert "final" not in [ev.event for ev in session_replay_source(session)]
    assert sid in provider.cancelled
    with pytest.raises(LogicError):
        hub.finish(sid)


def test_successful_start_still_finishes_once():
    provider = ScriptedProvider()
    hub = _hub(provider)
    sid = hub.create(RecognizeSessionCreate(language="zh")).session_id
    closed = hub.finish(sid)
    assert closed.reason.value == "finished"
    assert provider.finished == [sid]
    with pytest.raises(LogicError):
        hub.finish(sid)
