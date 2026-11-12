"""设置页异步作业：一次真实安装/下载/启动动作的状态与进度。

动作由调用方显式注入（生产入口给真实动作，测试给夹具）；未注入的动作明确拒绝，不返回假成功。
同类作业运行中再次请求，返回同一作业，不并发重复下载。
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from _contracts import (
    ErrorCode,
    LogicError,
    SetupProgress,
    SetupTask,
    SetupTaskKind,
    SetupTaskState,
)

ProgressFn = Callable[[SetupProgress], None]
SetupAction = Callable[[ProgressFn], str]
Spawn = Callable[[Callable[[], None]], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def thread_spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="setup-task", daemon=True).start()


class SetupTaskManager:
    def __init__(
        self,
        actions: Mapping[SetupTaskKind, SetupAction],
        *,
        spawn: Spawn = thread_spawn,
        clock: Callable[[], str] = _now_iso,
    ) -> None:
        self._actions = dict(actions)
        self._spawn = spawn
        self._clock = clock
        self._lock = threading.Lock()
        self._tasks: dict[str, SetupTask] = {}
        self._latest: dict[SetupTaskKind, str] = {}

    def available(self) -> frozenset[SetupTaskKind]:
        return frozenset(self._actions)

    def start(self, kind: SetupTaskKind) -> SetupTask:
        action = self._actions.get(kind)
        if action is None:
            raise LogicError(ErrorCode.dependency_failed, f"当前运行环境未提供动作 {kind.value}，拒绝假成功")
        with self._lock:
            current = self._current_unlocked(kind)
            if current is not None and current.state == SetupTaskState.running:
                return current
            task = SetupTask(
                task_id=uuid.uuid4().hex,
                kind=kind,
                state=SetupTaskState.running,
                message="已开始",
                started_at=self._clock(),
            )
            self._tasks[task.task_id] = task
            self._latest[kind] = task.task_id
        self._spawn(lambda: self._run(task.task_id, action))
        return self.get(task.task_id)

    def get(self, task_id: str) -> SetupTask:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise LogicError(ErrorCode.not_found, f"未知的设置作业 {task_id}")
        return task

    def latest(self, kind: SetupTaskKind) -> SetupTask | None:
        with self._lock:
            return self._current_unlocked(kind)

    def _current_unlocked(self, kind: SetupTaskKind) -> SetupTask | None:
        task_id = self._latest.get(kind)
        return self._tasks.get(task_id) if task_id else None

    def _update(self, task_id: str, **fields: object) -> None:
        with self._lock:
            task = self._tasks[task_id]
            self._tasks[task_id] = task.model_copy(update=fields)

    def _run(self, task_id: str, action: SetupAction) -> None:
        def report(progress: SetupProgress) -> None:
            self._update(task_id, progress=progress, message=progress.stage or "进行中")

        try:
            message = action(report)
        except LogicError as exc:
            self._update(
                task_id,
                state=SetupTaskState.failed,
                error=exc.code,
                message=exc.message,
                finished_at=self._clock(),
            )
            return
        except Exception as exc:  # 未预期异常也必须可见，不能让作业停在 running
            self._update(
                task_id,
                state=SetupTaskState.failed,
                error=ErrorCode.internal,
                message=f"{type(exc).__name__}: {exc}",
                finished_at=self._clock(),
            )
            return
        self._update(
            task_id,
            state=SetupTaskState.succeeded,
            error=None,
            message=message,
            finished_at=self._clock(),
        )
