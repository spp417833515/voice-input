"""最简实时优先调度：同一时刻只有一个官方推理。流式优先于整段转写。

不抢占已经开始的推理；新到的流式请求排在等待中的 batch 之前。
已有活动流式会话时，再开流式直接 conflict。不做多 runtime fallback。
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from _contracts import ErrorCode, LogicError

Kind = Literal["stream", "batch"]


@dataclass
class _Waiter:
    kind: Kind
    event: threading.Event
    cancelled: bool = False


class EngineScheduler:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._active: Kind | None = None
        self._active_stream_id: str | None = None
        self._waiters: deque[_Waiter] = deque()

    @property
    def active_stream_id(self) -> str | None:
        with self._guard:
            return self._active_stream_id

    def acquire_stream(self, session_id: str, *, timeout_s: float = 120.0) -> None:
        with self._guard:
            if self._active == "stream" or self._active_stream_id is not None:
                raise LogicError(ErrorCode.conflict, "已有识别会话在进行，sidecar 只支持单会话")
            if self._active is None:
                self._active = "stream"
                self._active_stream_id = session_id
                return
            waiter = _Waiter(kind="stream", event=threading.Event())
            self._waiters.appendleft(waiter)
        if not waiter.event.wait(timeout=timeout_s):
            waiter.cancelled = True
            raise LogicError(ErrorCode.timeout, "等待让出官方引擎超时")
        with self._guard:
            if self._active is not None:
                raise LogicError(ErrorCode.conflict, "官方引擎仍被占用")
            self._active = "stream"
            self._active_stream_id = session_id

    def acquire_batch(self, *, timeout_s: float = 120.0) -> None:
        with self._guard:
            if self._active is None and not self._waiters:
                self._active = "batch"
                return
            waiter = _Waiter(kind="batch", event=threading.Event())
            self._waiters.append(waiter)
        if not waiter.event.wait(timeout=timeout_s):
            waiter.cancelled = True
            raise LogicError(ErrorCode.timeout, "等待整段转写槽位超时")
        with self._guard:
            if self._active is not None:
                raise LogicError(ErrorCode.conflict, "官方引擎仍被占用")
            self._active = "batch"

    def release(self) -> None:
        with self._guard:
            self._active = None
            self._active_stream_id = None
            while self._waiters:
                waiter = self._waiters.popleft()
                if waiter.cancelled:
                    continue
                waiter.event.set()
                return

    def hold_stream(self, session_id: str) -> Any:
        return _Held(self, "stream", session_id)

    def hold_batch(self) -> Any:
        return _Held(self, "batch", None)


class _Held:
    def __init__(self, sched: EngineScheduler, kind: Kind, session_id: str | None) -> None:
        self._sched = sched
        self._kind = kind
        self._session_id = session_id

    def __enter__(self) -> EngineScheduler:
        if self._kind == "stream":
            assert self._session_id is not None
            self._sched.acquire_stream(self._session_id)
        else:
            self._sched.acquire_batch()
        return self._sched

    def __exit__(self, *exc: object) -> None:
        # 流式会话在 finish/cancel 前保持占用；batch 结束即释放。
        if self._kind == "batch":
            self._sched.release()
