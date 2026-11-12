"""Job 队列的业务规则：claim/ack、崩溃恢复与超时，不重投。

存取只通过 JobStore（含 update_if / put_if_absent）。认证与来源判定不在本模块。
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from _contracts import (
    AckResult,
    ErrorCode,
    JobAckRequest,
    JobClaimed,
    JobQueryFound,
    JobQueryUnknown,
    JobRecord,
    JobState,
    LogicError,
    ProductDeliveryUnknownEvent,
    ProductErrorEvent,
    ProductTypedEvent,
)
from _events import EventBus
from _jobs import JobStore

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class JobsService:
    def __init__(
        self,
        store: JobStore,
        events: EventBus,
        *,
        clock: Clock | None = None,
        claim_lease_s: float = 60.0,
    ) -> None:
        self._store = store
        self._events = events
        self._clock = clock or _utc_now
        self._claim_lease_s = claim_lease_s

    def get(self, job_id: str) -> JobQueryFound | JobQueryUnknown:
        if not job_id:
            raise LogicError(ErrorCode.invalid_request, "job_id 不能为空")
        record = self._store.get(job_id)
        if record is None:
            return JobQueryUnknown(job_id=job_id)
        return JobQueryFound(job=record)

    def enqueue(self, record: JobRecord) -> JobRecord:
        return self._store.put_if_absent(record)

    def find_by_session_id(self, session_id: str) -> JobRecord | None:
        """以已持久的 JobRecord.session_id 查证绑定，不另建索引库。"""
        if not session_id:
            return None
        matches = [job for job in self._store.list_jobs() if job.session_id == session_id]
        if not matches:
            return None
        if len(matches) > 1:
            raise LogicError(
                ErrorCode.conflict,
                "同一 session 存在多条 job 记录",
                details={"session_id": session_id, "job_ids": [job.job_id for job in matches]},
            )
        return matches[0]

    def claim(self, wait_s: float | None = None) -> JobClaimed | None:
        """原子 queued→claimed。无任务返回 None，不编造 job。wait_s 仅表示本次等待，不是重投。"""
        del wait_s  # 长轮询属于路由集成；本层只做一次扫描。
        self.expire_stale_claims()
        candidates = [
            job
            for job in self._store.list_jobs()
            if job.state == JobState.queued
        ]
        candidates.sort(key=lambda job: (job.created_at, job.job_id))
        now = _iso(self._clock())
        for job in candidates:
            claim_id = str(uuid.uuid4())
            try:
                updated = self._store.update_if(
                    job.job_id,
                    expected={"state": JobState.queued},
                    fields={"state": JobState.claimed, "claim_id": claim_id, "updated_at": now},
                )
            except LogicError as exc:
                if exc.code == ErrorCode.conflict:
                    continue
                raise
            return JobClaimed(
                job_id=updated.job_id,
                claim_id=updated.claim_id or claim_id,
                text=updated.text,
                source=updated.source,
                focus_id=updated.focus_id,
            )
        return None

    def ack(self, job_id: str, request: JobAckRequest) -> JobRecord:
        if not job_id:
            raise LogicError(ErrorCode.invalid_request, "job_id 不能为空")
        if not request.claim_id:
            raise LogicError(ErrorCode.invalid_request, "claim_id 不能为空")
        current = self._store.get(job_id)
        if current is None:
            raise LogicError(ErrorCode.not_found, f"job 不存在: {job_id}")
        if current.claim_id != request.claim_id:
            raise LogicError(
                ErrorCode.conflict,
                "claim_id 不匹配",
                details={"job_id": job_id},
            )
        if current.state == JobState.claimed:
            return self._apply_ack(current, request)
        if current.state == JobState.delivery_unknown:
            return self._apply_ack(current, request)
        raise LogicError(
            ErrorCode.conflict,
            f"当前状态不可 ack: {current.state}",
            details={"job_id": job_id, "state": current.state},
        )

    def _apply_ack(self, current: JobRecord, request: JobAckRequest) -> JobRecord:
        if request.result == AckResult.complete:
            new_state = JobState.done
        elif request.result == AckResult.none:
            new_state = JobState.failed
        elif request.result == AckResult.partial_or_unknown:
            new_state = JobState.delivery_unknown
        else:
            raise LogicError(ErrorCode.invalid_request, f"未知 ack result: {request.result}")
        now = _iso(self._clock())
        try:
            updated = self._store.update_if(
                current.job_id,
                expected={"state": current.state, "claim_id": current.claim_id},
                fields={
                    "state": new_state,
                    "updated_at": now,
                    "detail": request.detail,
                },
            )
        except LogicError as exc:
            if exc.code == ErrorCode.conflict:
                raise LogicError(
                    ErrorCode.conflict,
                    "ack 条件不成立，可能已被其他调用收敛",
                    details={"job_id": current.job_id},
                ) from exc
            raise
        if updated.state == JobState.done:
            self._events.publish(ProductTypedEvent(job_id=updated.job_id, text=updated.text))
        elif updated.state == JobState.failed:
            self._events.publish(
                ProductErrorEvent(
                    error=ErrorCode.delivery_failed,
                    message=request.detail or "未输入",
                    details={"job_id": updated.job_id},
                )
            )
        elif updated.state == JobState.delivery_unknown:
            self._events.publish(
                ProductDeliveryUnknownEvent(job_id=updated.job_id, text=updated.text)
            )
        return updated

    def expire_stale_claims(self, *, now: datetime | None = None) -> list[JobRecord]:
        """claimed 超过租约 → delivery_unknown，不退回 queued，不重投。"""
        moment = now or self._clock()
        changed: list[JobRecord] = []
        for job in self._store.list_jobs():
            if job.state != JobState.claimed:
                continue
            stamped = _parse_iso(job.updated_at) or _parse_iso(job.created_at)
            if stamped is None:
                unparsable = True
            else:
                unparsable = False
                if stamped.tzinfo is None:
                    stamped = stamped.replace(tzinfo=timezone.utc)
                if moment - stamped < timedelta(seconds=self._claim_lease_s):
                    continue
            detail = "claim timestamp unparsable" if unparsable else "claim timeout"
            try:
                updated = self._store.update_if(
                    job.job_id,
                    expected={"state": JobState.claimed, "claim_id": job.claim_id},
                    fields={
                        "state": JobState.delivery_unknown,
                        "updated_at": _iso(moment),
                        "detail": detail,
                    },
                )
            except LogicError as exc:
                if exc.code == ErrorCode.conflict:
                    continue
                raise
            self._events.publish(
                ProductDeliveryUnknownEvent(job_id=updated.job_id, text=updated.text)
            )
            changed.append(updated)
        return changed

    def recover(self) -> list[JobRecord]:
        """进程恢复：claimed 视为投递未知并记下，不把任何状态退回 queued，不从历史重建队列。"""
        changed: list[JobRecord] = []
        now = _iso(self._clock())
        for job in self._store.list_jobs():
            if job.state != JobState.claimed:
                continue
            try:
                updated = self._store.update_if(
                    job.job_id,
                    expected={"state": JobState.claimed, "claim_id": job.claim_id},
                    fields={
                        "state": JobState.delivery_unknown,
                        "updated_at": now,
                        "detail": "recovered after interruption; not redelivered",
                    },
                )
            except LogicError as exc:
                if exc.code == ErrorCode.conflict:
                    continue
                raise
            self._events.publish(
                ProductDeliveryUnknownEvent(job_id=updated.job_id, text=updated.text)
            )
            changed.append(updated)
        return changed
