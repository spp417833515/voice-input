"""B3：会话回放只保留有限快照，旧 partial 不得挤掉终态。"""
from __future__ import annotations

import gc
import threading

import pytest

from _contracts import (
    ErrorCode,
    LogicError,
    RecognizeSessionCreate,
    SessionErrorEvent,
    SessionPartialEvent,
)
from _session_finals import SessionFinalIndex
from _v3.sessions import (
    SESSION_REPLAY_MAX,
    SESSION_REPLAY_TERMINAL_MIN,
    RecognizeSessionHub,
    session_replay_source,
)
from tests.api.test_session_start_lifecycle import ScriptedProvider, _hub


def _partial(sid: str, n: int, *, stable: str = "") -> SessionPartialEvent:
    return SessionPartialEvent(
        session_id=sid,
        stable_text=stable,
        tail_text=f"t{n:05d}",
        language="zh",
        audio_end_s=float(n),
    )


def _drain(sub, *, limit: int = 32) -> list:
    events = []
    for _ in range(limit):
        if sub.empty() and events:
            break
        try:
            events.append(sub.get(timeout=0.2))
        except Exception:
            break
    return events


def test_sse_queue_max_below_terminal_capacity_is_rejected():
    with pytest.raises(LogicError) as caught:
        RecognizeSessionHub(
            finals=SessionFinalIndex(),
            provider=ScriptedProvider(),
            max_chunk_bytes=64_000,
            max_session_pcm_bytes=160_000,
            sse_queue_max=SESSION_REPLAY_TERMINAL_MIN - 1,
        )
    assert caught.value.code == ErrorCode.invalid_request
    assert "sse_queue_max" in caught.value.message


def test_10000_partials_memory_stays_at_fixed_bound():
    hub = _hub(ScriptedProvider())
    sid = hub.create(RecognizeSessionCreate(language="zh")).session_id
    session = hub._sessions[sid]
    gc.collect()
    for i in range(10_000):
        hub.publish_partial(sid, _partial(sid, i, stable=""))
    gc.collect()
    assert not hasattr(session, "emitted")
    source = session_replay_source(session)
    assert len(source) == 1
    assert source[0].event == "partial"
    assert source[0].tail_text == "t09999"
    assert source[0].stable_text == ""
    assert session.latest_partial is source[0]
    assert session.final is None and session.closed is None
    # 快照字段数有固定上界，与 10000 条无关
    assert len(source) <= SESSION_REPLAY_MAX
    closed = hub.finish(sid)
    assert closed.reason.value == "finished"
    source = session_replay_source(session)
    assert [ev.event for ev in source] == ["partial", "final", "closed"]
    assert source[0].tail_text == "t09999"
    assert source[1].text == "scripted-final"
    assert len(source) == 3
    assert len(source) <= SESSION_REPLAY_MAX
    # 终态后新 partial 拒绝，快照不变
    with pytest.raises(LogicError) as caught:
        hub.publish_partial(sid, _partial(sid, 10000))
    assert caught.value.code == ErrorCode.conflict
    assert session_replay_source(session) == source


def test_late_subscribe_after_20_partials_with_max_8_gets_terminal():
    hub = RecognizeSessionHub(
        finals=SessionFinalIndex(),
        provider=ScriptedProvider(),
        max_chunk_bytes=64_000,
        max_session_pcm_bytes=160_000,
        sse_queue_max=8,
    )
    sid = hub.create(RecognizeSessionCreate(language="zh")).session_id
    for i in range(20):
        hub.publish_partial(sid, _partial(sid, i))
    hub.finish(sid)
    source = session_replay_source(hub._sessions[sid])
    assert [ev.event for ev in source] == ["partial", "final", "closed"]
    assert source[0].tail_text == "t00019"
    sub = hub.subscribe(sid)
    assert sub.overflow is False
    events = _drain(sub)
    kinds = [ev.event for ev in events]
    assert "final" in kinds and "closed" in kinds
    assert kinds[-2:] == ["final", "closed"]
    assert kinds.count("final") == 1
    assert kinds.count("closed") == 1
    assert kinds.count("partial") == 1
    assert events[0].tail_text == "t00019"
    assert "t00000" not in [getattr(ev, "tail_text", "") for ev in events]
    hub.unsubscribe(sid, sub)
    # 再订一次也不重复、仍能拿到终态
    again = hub.subscribe(sid)
    kinds2 = [ev.event for ev in _drain(again)]
    assert kinds2 == kinds
    assert kinds2.count("final") == 1
    hub.unsubscribe(sid, again)


def test_late_subscribe_max_2_still_gets_final_and_closed_not_old_partials():
    hub = RecognizeSessionHub(
        finals=SessionFinalIndex(),
        provider=ScriptedProvider(),
        max_chunk_bytes=64_000,
        max_session_pcm_bytes=160_000,
        sse_queue_max=SESSION_REPLAY_TERMINAL_MIN,
    )
    sid = hub.create(RecognizeSessionCreate(language="zh")).session_id
    for i in range(20):
        hub.publish_partial(sid, _partial(sid, i))
    hub.finish(sid)
    sub = hub.subscribe(sid)
    events = _drain(sub)
    kinds = [ev.event for ev in events]
    assert kinds == ["final", "closed"]
    assert sub.overflow is False
    hub.unsubscribe(sid, sub)


def test_open_late_subscribe_gets_latest_partial_not_old_eight():
    hub = RecognizeSessionHub(
        finals=SessionFinalIndex(),
        provider=ScriptedProvider(),
        max_chunk_bytes=64_000,
        max_session_pcm_bytes=160_000,
        sse_queue_max=8,
    )
    sid = hub.create(RecognizeSessionCreate(language="zh")).session_id
    for i in range(20):
        hub.publish_partial(sid, _partial(sid, i, stable="stable-now" if i == 19 else ""))
    sub = hub.subscribe(sid)
    events = _drain(sub)
    assert len(events) == 1
    assert events[0].event == "partial"
    assert events[0].tail_text == "t00019"
    assert events[0].stable_text == "stable-now"
    tails = [getattr(ev, "tail_text", "") for ev in events]
    assert tails != [f"t{i:05d}" for i in range(8)]
    hub.unsubscribe(sid, sub)
    hub.cancel(sid)


def test_error_branch_late_subscribe_gets_error_and_closed():
    hub = _hub(ScriptedProvider())
    sid = hub.create(RecognizeSessionCreate(language="zh")).session_id
    hub.publish_partial(sid, _partial(sid, 1))
    hub.publish_provider_error(
        sid,
        SessionErrorEvent(session_id=sid, error=ErrorCode.asr_unavailable, message="引擎失败"),
    )
    assert hub._sessions[sid].state == "error"
    assert hub._sessions[sid].final is None
    sub = hub.subscribe(sid)
    kinds = [ev.event for ev in _drain(sub)]
    assert kinds[0] == "partial"
    assert kinds[-2:] == ["error", "closed"]
    assert "final" not in kinds
    hub.unsubscribe(sid, sub)
    with pytest.raises(LogicError):
        hub.finish(sid)


def test_cancel_branch_late_subscribe_has_closed_without_final_and_does_not_finish():
    hub = _hub(ScriptedProvider())
    sid = hub.create(RecognizeSessionCreate(language="zh")).session_id
    hub.publish_partial(sid, _partial(sid, 3))
    closed = hub.cancel(sid)
    assert closed.reason.value == "cancelled"
    assert hub._sessions[sid].final is None
    sub = hub.subscribe(sid)
    events = _drain(sub)
    kinds = [ev.event for ev in events]
    assert "final" not in kinds
    assert kinds[-1] == "closed"
    assert events[-1].reason.value == "cancelled"
    assert kinds.count("closed") == 1
    hub.unsubscribe(sid, sub)
    assert hub._provider.finished == []


def test_subscribe_and_publish_concurrent_no_deadlock_or_duplicate_final():
    hub = _hub(ScriptedProvider())
    sid = hub.create(RecognizeSessionCreate(language="zh")).session_id
    seen: list = []
    errors: list[BaseException] = []
    start = threading.Event()

    def _publish() -> None:
        start.wait(timeout=2.0)
        for i in range(40):
            hub.publish_partial(sid, _partial(sid, i))
        hub.finish(sid)

    def _subscribe() -> None:
        start.wait(timeout=2.0)
        try:
            sub = hub.subscribe(sid)
            events = []
            for _ in range(50):
                try:
                    ev = sub.get(timeout=1.0)
                except Exception:
                    break
                events.append(ev)
                if getattr(ev, "event", None) == "closed":
                    break
            seen.extend(events)
            hub.unsubscribe(sid, sub)
        except Exception as exc:
            errors.append(exc)

    workers = [
        threading.Thread(target=_publish, name="b3-pub"),
        threading.Thread(target=_subscribe, name="b3-sub-a"),
        threading.Thread(target=_subscribe, name="b3-sub-b"),
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(timeout=5.0)
        assert not worker.is_alive()
    assert errors == []
    assert hub._sessions[sid].state == "finished"
    late = hub.subscribe(sid)
    late_events = _drain(late)
    late_kinds = [ev.event for ev in late_events]
    assert late_kinds[-2:] == ["final", "closed"]
    assert late_kinds.count("final") == 1
    hub.unsubscribe(sid, late)


def test_live_overflow_stays_visible_and_does_not_drop_key_events_silently():
    hub = RecognizeSessionHub(
        finals=SessionFinalIndex(),
        provider=ScriptedProvider(),
        max_chunk_bytes=64_000,
        max_session_pcm_bytes=160_000,
        sse_queue_max=8,
    )
    sid = hub.create(RecognizeSessionCreate(language="zh")).session_id
    sub = hub.subscribe(sid)
    for i in range(20):
        hub.publish_partial(sid, _partial(sid, i))
    assert sub.overflow is True
    queued = []
    while not sub.empty():
        queued.append(sub.get_nowait())
    assert len(queued) <= 8
    assert all(ev.event == "partial" for ev in queued)
    # finish 时 live 队列已满，final 进不去也必须保持 overflow 可见，不得悄悄丢掉终态却装作成功。
    hub.finish(sid)
    assert sub.overflow is True
    live_kinds = [ev.event for ev in queued]
    assert "final" not in live_kinds
    hub.unsubscribe(sid, sub)
    # 晚订阅仍然能从有限快照拿到终态
    late = hub.subscribe(sid)
    late_kinds = [ev.event for ev in _drain(late)]
    assert late_kinds[-2:] == ["final", "closed"]
    assert late.overflow is False
    hub.unsubscribe(sid, late)


def test_sweep_recycles_snapshot_fields():
    class Clock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            return self.t

    clock = Clock()
    hub = RecognizeSessionHub(
        finals=SessionFinalIndex(),
        provider=ScriptedProvider(),
        max_chunk_bytes=64_000,
        max_session_pcm_bytes=160_000,
        clock=clock,
        retain_closed_s=10.0,
    )
    sid = hub.create(RecognizeSessionCreate(language="zh")).session_id
    hub.publish_partial(sid, _partial(sid, 1))
    hub.finish(sid)
    assert hub.has_session(sid)
    clock.t = 11.0
    swept = hub.sweep()
    assert sid in swept["evicted"]
    assert not hub.has_session(sid)
