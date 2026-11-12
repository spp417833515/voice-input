"""识别会话编排。依赖 ASR 提供者接口，不是识别引擎，不做字幕拼接。"""
from __future__ import annotations

import hashlib
import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

from _contracts import (
    ChunkAccepted,
    ErrorCode,
    LogicError,
    ProductErrorEvent,
    ProductIdleEvent,
    ProductPartialEvent,
    ProductRecordingEvent,
    RecognizeSessionCreate,
    RecognizeSessionCreated,
    SessionClosedEvent,
    SessionClosedReason,
    SessionErrorEvent,
    SessionFinalEvent,
    SessionPartialEvent,
    SessionSSEEvent,
)
from _events import EventBus
from _session_finals import SessionFinalIndex

# 晚订阅回放的必要容量：唯一终态（final 或 error）+ closed。字幕只保留最新一条快照。
SESSION_REPLAY_TERMINAL_MIN = 2
SESSION_REPLAY_MAX = 3  # latest partial + terminal + closed


class SessionCaptionSink(Protocol):
    """接收流式识别的异步字幕与可见错误。不是第二套识别实现。"""

    def publish_partial(self, session_id: str, event: SessionPartialEvent) -> None: ...

    def publish_provider_error(self, session_id: str, event: SessionErrorEvent) -> None: ...


class StreamingAsrProvider(Protocol):
    """流式识别端口。实现可以是 sidecar 客户端或测试夹具；本层不把夹具当官方引擎。

    start 可接收 sink，供后台事件消费者投递 mid-stream partial。
    push_pcm 仍可同步返回至多一个 partial（夹具）；真实 sidecar 应返回 None，由 sink 投递 0..N 个事件。
    同一条字幕不得既通过返回值又通过 sink 投递。
    """

    def start(
        self,
        session_id: str,
        body: RecognizeSessionCreate,
        *,
        sink: SessionCaptionSink | None = None,
    ) -> None: ...

    def push_pcm(self, session_id: str, index: int, pcm: bytes) -> SessionPartialEvent | None: ...

    def finish(self, session_id: str) -> SessionFinalEvent: ...

    def cancel(self, session_id: str) -> None: ...


@dataclass
class _Chunk:
    index: int
    digest: str
    size: int


@dataclass
class SessionSubscriber:
    """有界 SSE 订阅。队列满时置 overflow，由发送端可见终止，不静默丢事件。"""

    queue: queue.Queue[SessionSSEEvent]
    overflow: bool = False

    def get(self, block: bool = True, timeout: float | None = None) -> SessionSSEEvent:
        return self.queue.get(block, timeout)

    def get_nowait(self) -> SessionSSEEvent:
        return self.queue.get_nowait()

    def empty(self) -> bool:
        return self.queue.empty()

    def qsize(self) -> int:
        return self.queue.qsize()


@dataclass
class RecognizeSession:
    session_id: str
    language: str | None
    context: str | None
    state: str = "open"
    next_index: int = 0
    chunks: dict[int, _Chunk] = field(default_factory=dict)
    pcm_bytes: int = 0
    subscribers: list[SessionSubscriber] = field(default_factory=list)
    latest_partial: SessionPartialEvent | None = None
    final: SessionFinalEvent | None = None
    error: SessionErrorEvent | None = None
    closed: SessionClosedEvent | None = None
    created_at: float = 0.0
    last_activity: float = 0.0
    closed_at: float | None = None
    provider_ready: bool = False


def session_replay_source(session: RecognizeSession) -> list[SessionSSEEvent]:
    """同一会话内固定有限字段的回放源：最新字幕快照、唯一终态（final 或 error）、closed。

    不是事件日志，不拼接 stable_text/tail_text，不保留历史 partial。
    """
    items: list[SessionSSEEvent] = []
    if session.latest_partial is not None:
        items.append(session.latest_partial)
    if session.final is not None:
        items.append(session.final)
    elif session.error is not None:
        items.append(session.error)
    if session.closed is not None:
        items.append(session.closed)
    return items


class RecognizeSessionHub:
    def __init__(
        self,
        *,
        finals: SessionFinalIndex,
        provider: StreamingAsrProvider | None,
        max_chunk_bytes: int,
        max_session_pcm_bytes: int,
        clock: Callable[[], float] = time.monotonic,
        retain_closed_s: float = 60.0,
        idle_open_s: float = 180.0,
        sse_queue_max: int = 32,
        product_events: EventBus | None = None,
    ) -> None:
        self._product_events = product_events
        self._finals = finals
        self._provider = provider
        self._max_chunk_bytes = max_chunk_bytes
        self._max_session_pcm_bytes = max_session_pcm_bytes
        self._clock = clock
        self._retain_closed_s = retain_closed_s
        self._idle_open_s = idle_open_s
        if sse_queue_max < SESSION_REPLAY_TERMINAL_MIN:
            raise LogicError(
                ErrorCode.invalid_request,
                f"sse_queue_max 必须至少 {SESSION_REPLAY_TERMINAL_MIN}，才能回放终态 final/error 与 closed",
                details={"sse_queue_max": sse_queue_max, "minimum": SESSION_REPLAY_TERMINAL_MIN},
            )
        self._sse_queue_max = sse_queue_max
        self._guard = threading.RLock()
        self._sessions: dict[str, RecognizeSession] = {}

    def create(self, body: RecognizeSessionCreate) -> RecognizeSessionCreated:
        if self._provider is None:
            raise LogicError(
                ErrorCode.asr_unavailable,
                "未注入流式识别提供者，不能创建会话。夹具或 sidecar 均须显式提供；不得假装官方引擎可用。",
            )
        self.sweep()
        session_id = str(uuid.uuid4())
        now = self._clock()
        # 先登记本地身份，再启动外部 provider：start 期间到达的 partial/error 有处可去。
        # 不持锁调用 provider，避免等外部 IO / 读线程回调时死锁。
        with self._guard:
            self._sessions[session_id] = RecognizeSession(
                session_id=session_id,
                language=body.language,
                context=body.context,
                created_at=now,
                last_activity=now,
                provider_ready=False,
            )
        try:
            self._provider.start(session_id, body, sink=self)
        except Exception as exc:
            self._rollback_start(session_id, exc)
            if isinstance(exc, LogicError):
                raise
            raise LogicError(ErrorCode.asr_unavailable, f"识别会话启动失败: {exc}") from exc
        current = None
        ready = False
        with self._guard:
            current = self._sessions.get(session_id)
            if current is not None and current.state == "open":
                current.provider_ready = True
                current.last_activity = self._clock()
                ready = True
        if ready:
            self._mirror_product(ProductRecordingEvent(session_id=session_id))
            return RecognizeSessionCreated(session_id=session_id)
        # start 已返回，但会话已在启动过程中被 cancel/error 关闭：不得 200
        ended = current.state if current is not None else "missing"
        self._rollback_start(
            session_id,
            LogicError(
                ErrorCode.conflict,
                f"识别会话在启动完成前已结束，不能当作已创建: {ended}",
                details={"session_id": session_id, "state": ended},
            ),
        )
        raise LogicError(
            ErrorCode.conflict,
            f"识别会话在启动完成前已结束，不能当作已创建: {ended}",
            details={"session_id": session_id, "state": ended},
        )

    def has_session(self, session_id: str) -> bool:
        with self._guard:
            return session_id in self._sessions

    def _get(self, session_id: str) -> RecognizeSession:
        with self._guard:
            session = self._sessions.get(session_id)
            if session is None:
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            return session

    def accept_chunk(self, session_id: str, index: int, pcm: bytes) -> ChunkAccepted:
        if index < 0:
            raise LogicError(ErrorCode.invalid_request, "chunk index 不能为负")
        if len(pcm) == 0:
            raise LogicError(ErrorCode.invalid_request, "chunk 不能为空")
        if len(pcm) % 2 != 0:
            raise LogicError(ErrorCode.invalid_request, "音频编码必须是 s16le PCM，长度为偶数")
        if len(pcm) > self._max_chunk_bytes:
            raise LogicError(
                ErrorCode.invalid_request,
                "chunk 超过允许大小",
                details={"max_chunk_bytes": self._max_chunk_bytes, "size": len(pcm)},
            )
        self.sweep()
        session = self._get(session_id)
        with self._guard:
            if session.state != "open":
                raise LogicError(ErrorCode.conflict, f"会话已结束，不能再接收 chunk: {session.state}")
            if not session.provider_ready:
                raise LogicError(ErrorCode.conflict, "会话尚未就绪，不能接收 chunk")
            digest = hashlib.sha256(pcm).hexdigest()
            existing = session.chunks.get(index)
            if existing is not None:
                if existing.digest != digest:
                    raise LogicError(ErrorCode.conflict, "同一 chunk index 但内容不同")
                return ChunkAccepted(index=index)
            if index != session.next_index:
                raise LogicError(
                    ErrorCode.conflict,
                    "chunk 顺序错误",
                    details={"expected": session.next_index, "index": index},
                )
            if session.pcm_bytes + len(pcm) > self._max_session_pcm_bytes:
                raise LogicError(ErrorCode.invalid_request, "会话音频总长度超过上限")
            session.chunks[index] = _Chunk(index=index, digest=digest, size=len(pcm))
            session.pcm_bytes += len(pcm)
            session.next_index = index + 1
            session.last_activity = self._clock()
        if self._provider is None:
            raise LogicError(ErrorCode.asr_unavailable, "未注入流式识别提供者")
        maybe_partial = self._provider.push_pcm(session_id, index, pcm)
        if maybe_partial is not None:
            self.publish_partial(session_id, maybe_partial)
        return ChunkAccepted(index=index)

    def finish(self, session_id: str) -> SessionClosedEvent:
        session = self._get(session_id)
        with self._guard:
            if session.state != "open":
                raise LogicError(ErrorCode.conflict, f"会话已结束，不能重复 finish: {session.state}")
            if not session.provider_ready:
                raise LogicError(ErrorCode.conflict, "会话尚未就绪，不能 finish")
        if self._provider is None:
            raise LogicError(ErrorCode.asr_unavailable, "未注入流式识别提供者")
        final = self._provider.finish(session_id)
        if not isinstance(final, SessionFinalEvent):
            raise LogicError(ErrorCode.asr_unavailable, "finish 必须返回 final 事件")
        self._finals.record_final(session_id, final.text, final.language)
        self._publish(session, final)
        closed = SessionClosedEvent(session_id=session_id, reason=SessionClosedReason.finished)
        self._close(session, closed)
        return closed

    def cancel(self, session_id: str) -> SessionClosedEvent:
        session = self._get(session_id)
        with self._guard:
            if session.state != "open":
                raise LogicError(ErrorCode.conflict, f"会话已结束，不能 cancel: {session.state}")
        if self._provider is not None:
            self._provider.cancel(session_id)
        self._finals.record_closed(session_id, SessionClosedReason.cancelled)
        closed = SessionClosedEvent(session_id=session_id, reason=SessionClosedReason.cancelled)
        self._close(session, closed)
        return closed

    def subscribe(self, session_id: str) -> SessionSubscriber:
        session = self._get(session_id)
        sub = SessionSubscriber(queue=queue.Queue(maxsize=self._sse_queue_max))
        with self._guard:
            # 同一把锁里快照回放再挂订阅，不留缝。终态优先于旧字幕，不按日志从旧到新截断。
            replay = self._replay_for_queue(session)
            for event in replay:
                try:
                    sub.queue.put_nowait(event)
                except queue.Full:
                    raise LogicError(
                        ErrorCode.internal,
                        "有界回放无法写入订阅队列，拒绝造成功",
                        details={"sse_queue_max": self._sse_queue_max, "replay": [getattr(item, "event", None) for item in replay]},
                    )
            session.subscribers.append(sub)
        return sub

    def unsubscribe(self, session_id: str, subscriber: SessionSubscriber) -> None:
        try:
            session = self._get(session_id)
        except LogicError:
            return
        with self._guard:
            session.subscribers = [item for item in session.subscribers if item is not subscriber]

    def mark_disconnected(self, session_id: str) -> None:
        session = self._get(session_id)
        with self._guard:
            if session.state != "open":
                return
        if self._provider is not None:
            try:
                self._provider.cancel(session_id)
            except LogicError as exc:
                if exc.code != ErrorCode.not_found:
                    self._publish(
                        session,
                        SessionErrorEvent(
                            session_id=session_id,
                            error=exc.code,
                            message=f"断连清理识别提供者失败: {exc.message}",
                            details=exc.details,
                        ),
                    )
        error = SessionErrorEvent(
            session_id=session_id,
            error=ErrorCode.invalid_request,
            message="SSE 在 finish 前断开，不调用业务、不注入",
        )
        # 重新读取：cancel 路径不得把已关闭会话再关一次
        session = self._get(session_id)
        with self._guard:
            if session.state != "open":
                return
        self._publish(session, error)
        closed = SessionClosedEvent(session_id=session_id, reason=SessionClosedReason.disconnected)
        self._finals.record_closed(session_id, SessionClosedReason.disconnected)
        self._close(session, closed)

    def iter_events(self, session_id: str) -> Iterator[SessionSSEEvent]:
        sub = self.subscribe(session_id)
        try:
            while True:
                if sub.overflow:
                    yield SessionErrorEvent(
                        session_id=session_id,
                        error=ErrorCode.internal,
                        message="SSE 队列已满，连接已断开；请查询会话状态",
                    )
                    return
                event = sub.get()
                yield event
                if getattr(event, "event", None) == "closed":
                    return
        except GeneratorExit:
            self.mark_disconnected(session_id)
            raise
        finally:
            self.unsubscribe(session_id, sub)

    def evict(self, session_id: str) -> bool:
        """仅驱逐已关闭会话。开放会话须先 finish/cancel/disconnect。"""
        with self._guard:
            session = self._sessions.get(session_id)
            if session is None or session.closed is None:
                return False
            del self._sessions[session_id]
            return True

    def sweep(self, now: float | None = None) -> dict[str, list[str]]:
        """驱逐已关闭且超过保留期的会话；空闲过久的开放会话按 disconnect 关闭。时钟可注入，测试不要 sleep。"""
        ts = self._clock() if now is None else now
        evicted: list[str] = []
        idle: list[str] = []
        with self._guard:
            for sid, session in list(self._sessions.items()):
                if session.closed is not None and session.closed_at is not None:
                    if ts - session.closed_at >= self._retain_closed_s:
                        del self._sessions[sid]
                        evicted.append(sid)
                elif session.state == "open" and ts - session.last_activity >= self._idle_open_s:
                    idle.append(sid)
        for sid in idle:
            self.mark_disconnected(sid)
        return {"evicted": evicted, "idle_disconnected": idle}

    def publish_partial(self, session_id: str, event: SessionPartialEvent) -> None:
        """把 mid-stream partial 交给已有 SSE 订阅。不发明第二套识别。"""
        session = self._get(session_id)
        if session.state != "open":
            raise LogicError(ErrorCode.conflict, f"会话已结束，不能再发 partial: {session.state}")
        if not isinstance(event, SessionPartialEvent):
            raise LogicError(ErrorCode.internal, "publish_partial 只接受 SessionPartialEvent")
        if event.session_id != session_id:
            event = event.model_copy(update={"session_id": session_id})
        self._publish(session, event)

    def publish_provider_error(self, session_id: str, event: SessionErrorEvent) -> None:
        """识别提供者可见错误：投递 error 并以 error 关闭会话，不 finish、不入队。"""
        try:
            session = self._get(session_id)
        except LogicError:
            return
        with self._guard:
            if session.state != "open":
                return
        if not isinstance(event, SessionErrorEvent):
            raise LogicError(ErrorCode.internal, "publish_provider_error 只接受 SessionErrorEvent")
        if event.session_id != session_id:
            event = event.model_copy(update={"session_id": session_id})
        self._publish(session, event)
        self._finals.record_closed(session_id, SessionClosedReason.error)
        self._close(session, SessionClosedEvent(session_id=session_id, reason=SessionClosedReason.error))

    def _rollback_start(self, session_id: str, cause: BaseException) -> None:
        """启动失败或启动返回前已终态：取消外部 provider，并把仍 open 的会话收到明确终态。不持锁做外部 IO。"""
        if self._provider is not None:
            try:
                self._provider.cancel(session_id)
            except LogicError as exc:
                if exc.code != ErrorCode.not_found:
                    try:
                        session = self._get(session_id)
                    except LogicError:
                        session = None
                    if session is not None and session.state == "open":
                        self._publish(
                            session,
                            SessionErrorEvent(
                                session_id=session_id,
                                error=exc.code,
                                message=f"启动失败后取消识别提供者失败: {exc.message}",
                                details=exc.details,
                            ),
                        )
            except Exception as exc:
                try:
                    session = self._get(session_id)
                except LogicError:
                    session = None
                if session is not None and session.state == "open":
                    self._publish(
                        session,
                        SessionErrorEvent(
                            session_id=session_id,
                            error=ErrorCode.asr_unavailable,
                            message=f"启动失败后取消识别提供者失败: {exc}",
                        ),
                    )
        try:
            session = self._get(session_id)
        except LogicError:
            return
        with self._guard:
            if session.state != "open":
                return
        if isinstance(cause, LogicError):
            code, message, details = cause.code, cause.message, cause.details
        else:
            code, message, details = ErrorCode.asr_unavailable, f"识别会话启动失败: {cause}", None
        self._publish(
            session,
            SessionErrorEvent(session_id=session_id, error=code, message=message, details=details),
        )
        self._finals.record_closed(session_id, SessionClosedReason.error)
        self._close(session, SessionClosedEvent(session_id=session_id, reason=SessionClosedReason.error))

    def _replay_for_queue(self, session: RecognizeSession) -> list[SessionSSEEvent]:
        """在持锁调用。终态（final 或 error，加 closed）必须全部纳入；空间不够时放弃最新 partial，不得假装回放成功。"""
        source = session_replay_source(session)
        terminal = [item for item in source if getattr(item, "event", None) in {"final", "error", "closed"}]
        caption = [item for item in source if getattr(item, "event", None) == "partial"]
        if len(terminal) > self._sse_queue_max:
            raise LogicError(
                ErrorCode.internal,
                f"sse_queue_max={self._sse_queue_max} 不足以回放终态（需要 {len(terminal)}）",
                details={"sse_queue_max": self._sse_queue_max, "terminal": [getattr(item, "event", None) for item in terminal]},
            )
        if len(caption) + len(terminal) <= self._sse_queue_max:
            return caption + terminal
        return terminal

    def _retain_snapshot(self, session: RecognizeSession, event: SessionSSEEvent) -> None:
        """只更新固定字段。partial 是状态快照替换，不追加、不拼接。"""
        kind = getattr(event, "event", None)
        if kind == "partial":
            if not isinstance(event, SessionPartialEvent):
                raise LogicError(ErrorCode.internal, "partial 快照必须是 SessionPartialEvent")
            session.latest_partial = event
        elif kind == "final":
            if not isinstance(event, SessionFinalEvent):
                raise LogicError(ErrorCode.internal, "final 必须是 SessionFinalEvent")
            session.final = event
        elif kind == "error":
            if not isinstance(event, SessionErrorEvent):
                raise LogicError(ErrorCode.internal, "error 必须是 SessionErrorEvent")
            session.error = event
        elif kind == "closed":
            if not isinstance(event, SessionClosedEvent):
                raise LogicError(ErrorCode.internal, "closed 必须是 SessionClosedEvent")
            session.closed = event
        else:
            raise LogicError(ErrorCode.internal, f"会话不能保留未知事件: {kind!r}")

    def _publish(self, session: RecognizeSession, event: SessionSSEEvent) -> None:
        with self._guard:
            self._retain_snapshot(session, event)
            targets = list(session.subscribers)
        for item in targets:
            try:
                item.queue.put_nowait(event)
            except queue.Full:
                item.overflow = True
        self._mirror_session_event(event)

    def _mirror_session_event(self, event: SessionSSEEvent) -> None:
        """会话字幕/错误/非定稿关闭同步进产品事件流（状态栏唯一来源）；定稿后的状态由 utterance 业务发布。"""
        if isinstance(event, SessionPartialEvent):
            self._mirror_product(
                ProductPartialEvent(
                    session_id=event.session_id, stable_text=event.stable_text, tail_text=event.tail_text
                )
            )
        elif isinstance(event, SessionErrorEvent):
            self._mirror_product(ProductErrorEvent(error=event.error, message=event.message, details=event.details))
        elif isinstance(event, SessionClosedEvent) and event.reason in (
            SessionClosedReason.cancelled,
            SessionClosedReason.disconnected,
            SessionClosedReason.timeout,
        ):
            self._mirror_product(ProductIdleEvent())

    def _mirror_product(self, event) -> None:
        if self._product_events is not None:
            self._product_events.publish(event)

    def _close(self, session: RecognizeSession, closed: SessionClosedEvent) -> None:
        with self._guard:
            session.state = closed.reason.value
            session.closed = closed
            session.closed_at = self._clock()
        self._publish(session, closed)
