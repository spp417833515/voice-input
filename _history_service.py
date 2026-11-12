"""历史写入的唯一业务入口：进程内串行化。不把 final_text 当成投递成功。"""
from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from pathlib import Path

from _contracts import ErrorCode, HistoryListResponse, HistoryRecord, LogicError
from _history import HistoryStore

_WORKER_ENV_KEYS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "OLLAMA_VOICE_WORKERS")


def detect_multi_worker(environ: Mapping[str, str] | None = None) -> str | None:
    """若配置了多 worker，返回说明字符串；单进程返回 None。不得假装跨进程写安全。"""
    env = os.environ if environ is None else environ
    for key in _WORKER_ENV_KEYS:
        raw = env.get(key)
        if raw is None or raw.strip() == "":
            continue
        try:
            count = int(raw)
        except ValueError:
            continue
        if count > 1:
            return f"{key}={count}"
    return None


class HistoryService:
    """只包装 HistoryStore 的写/读。多 worker 在构造时显式拒绝。

    进程内锁不构成跨进程追加保证；未设置 worker 环境变量的第二进程不会在构造期被拒绝。
    """

    def __init__(
        self,
        store: HistoryStore,
        *,
        environ: Mapping[str, str] | None = None,
        worker_count: int | None = None,
    ) -> None:
        if worker_count is not None and worker_count > 1:
            raise LogicError(
                ErrorCode.internal,
                "历史写入只支持单进程串行化，当前 worker_count="
                f"{worker_count}。跨进程多 worker 不受本服务保护，请改为单 worker。",
                details={"worker_count": worker_count},
            )
        detected = detect_multi_worker(environ)
        if detected is not None:
            raise LogicError(
                ErrorCode.internal,
                "历史写入只支持单进程串行化，检测到 "
                f"{detected}。本服务不会假装跨进程追加安全。",
                details={"detected": detected},
            )
        self._store = store
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._store.path

    def append(self, record: HistoryRecord) -> None:
        with self._lock:
            self._store.append(record)

    def list_records(self, limit: int = 50, offset: int = 0) -> HistoryListResponse:
        with self._lock:
            return self._store.list_records(limit=limit, offset=offset)

    def _all_unlocked(self) -> list[HistoryRecord]:
        # 只为幂等查重扫描处理记录，不从历史重建投递队列。
        return self._store.list_records(limit=10_000_000, offset=0).records

    def record_for_job(self, job_id: str) -> HistoryRecord | None:
        """已有 job_id 的处理记录；旧记录若无 job_id 则不会被当作命中。"""
        if not job_id:
            return None
        with self._lock:
            for rec in self._all_unlocked():
                if rec.job_id == job_id:
                    return rec
        return None

    def record_for_session(self, session_id: str) -> HistoryRecord | None:
        """用处理记录上的 session_id 查证（extra 或字段）。不是第二套绑定库。"""
        if not session_id:
            return None
        with self._lock:
            for rec in self._all_unlocked():
                dumped = rec.model_dump()
                if dumped.get("session_id") == session_id:
                    return rec
        return None

    def append_for_job(self, record: HistoryRecord) -> HistoryRecord:
        """按 job_id 幂等追加：已有该 job_id 的处理记录则不再写入。无 job_id 的旧路径仍每次追加。"""
        if not record.job_id:
            self.append(record)
            return record
        with self._lock:
            existing = None
            for rec in self._all_unlocked():
                if rec.job_id == record.job_id:
                    existing = rec
                    break
            if existing is not None:
                return existing
            self._store.append(record)
            return record
