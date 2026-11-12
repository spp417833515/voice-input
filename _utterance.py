"""唯一业务编排：final → 可选 polish → 先写历史再入队。

桌面 session final、手机 from-audio、原文 resend 共用 _commit。
公开 STT 只转写，不走本编排。不在此判定请求来源鉴权，不拼接长音频。
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from _config import ConfigStore
from _contracts import (
    AppConfig,
    ErrorCode,
    HistoryPolish,
    HistoryRecord,
    JobQueryUnknown,
    JobRecord,
    JobState,
    LogicError,
    PolishRequest,
    PolishResponse,
    ProductErrorEvent,
    ProductHistoryWriteFailedEvent,
    ProductPolishingEvent,
    SessionClosedReason,
    SttResponse,
    UtteranceCreate,
    UtteranceFromAudio,
    UtteranceResendNeedsConfirmation,
    UtteranceResendQueued,
    UtteranceResendRequest,
    UtteranceSource,
)
from _events import EventBus
from _history_service import HistoryService
from _jobs_service import JobsService
from _session_finals import SessionFinalIndex

PolishFn = Callable[..., PolishResponse]
TranscribeFn = Callable[..., SttResponse]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def compute_payload_hash(fields: dict[str, object]) -> str:
    blob = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _record_hash(record: JobRecord) -> str | None:
    dumped = record.model_dump()
    value = dumped.get("payload_hash")
    return value if isinstance(value, str) else None


def _with_hash(record: JobRecord, payload_hash: str) -> JobRecord:
    data = record.model_dump()
    data["payload_hash"] = payload_hash
    return JobRecord.model_validate(data)


def _history_with_meta(
    record: HistoryRecord,
    *,
    payload_hash: str,
    session_id: str | None,
) -> HistoryRecord:
    data = record.model_dump()
    data["payload_hash"] = payload_hash
    if session_id:
        data["session_id"] = session_id
    return HistoryRecord.model_validate(data)


class UtteranceService:
    def __init__(
        self,
        *,
        jobs: JobsService,
        history: HistoryService,
        events: EventBus,
        config: ConfigStore,
        finals: SessionFinalIndex,
        polish: PolishFn | None = None,
        transcribe: TranscribeFn | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._jobs = jobs
        self._history = history
        self._events = events
        self._config = config
        self._finals = finals
        self._polish = polish
        self._transcribe = transcribe
        self._clock = clock or _utc_now
        self._guard = threading.RLock()
        self._job_locks: dict[str, threading.Lock] = {}
        self._session_locks: dict[str, threading.Lock] = {}

    def _lock_for(self, job_id: str) -> threading.Lock:
        with self._guard:
            lock = self._job_locks.get(job_id)
            if lock is None:
                lock = threading.Lock()
                self._job_locks[job_id] = lock
            return lock

    def _lock_for_session(self, session_id: str) -> threading.Lock:
        with self._guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _bound_job_id_for_session(self, session_id: str) -> str | None:
        """查证顺序：已持久 JobRecord.session_id，再处理历史，最后才是进程内索引。"""
        persisted = self._jobs.find_by_session_id(session_id)
        if persisted is not None:
            return persisted.job_id
        hist = self._history.record_for_session(session_id)
        if hist is not None and hist.job_id:
            return hist.job_id
        snap = self._finals.lookup(session_id)
        if snap is not None and snap.bound_job_id:
            return snap.bound_job_id
        return None

    def transcribe_public(
        self,
        audio: bytes,
        *,
        language: str | None = None,
        context: str | None = None,
    ) -> SttResponse:
        """对外纯转写：不 polish、不写历史、不入队。"""
        if not audio:
            raise LogicError(ErrorCode.invalid_request, "音频不能为空")
        if self._transcribe is None:
            raise LogicError(ErrorCode.asr_unavailable, "未注入转写能力，无法公开转写")
        return self._transcribe(audio, language=language, context=context)

    def submit_session_final(self, request: UtteranceCreate) -> JobRecord:
        """用户松开或看门狗 finish 都走这里；不取消已定稿的其他句子。

        同一 session final 只绑定一个 job_id。崩溃后以 JobRecord.session_id 为准。
        """
        if request.source == UtteranceSource.resend:
            raise LogicError(ErrorCode.invalid_request, "session final 不能使用 source=resend")
        snap = self._finals.lookup(request.session_id)
        if snap is None or not snap.has_final:
            raise LogicError(ErrorCode.invalid_request, "无 final，不入队")
        if snap.closed_reason == SessionClosedReason.cancelled:
            raise LogicError(ErrorCode.invalid_request, "会话已取消，不入队")
        if not snap.text.strip():
            raise LogicError(ErrorCode.invalid_request, "final 文本为空，不入队")
        with self._lock_for_session(request.session_id):
            bound = self._bound_job_id_for_session(request.session_id)
            if bound is not None and bound != request.job_id:
                raise LogicError(
                    ErrorCode.conflict,
                    "该 session final 已绑定其他 job_id，不能换 id 绕过",
                    details={"session_id": request.session_id, "bound_job_id": bound, "job_id": request.job_id},
                )
            self._finals.bind_job(request.session_id, request.job_id)
            return self._commit(
                job_id=request.job_id,
                raw_text=snap.text,
                language=snap.language or "",
                source=request.source,
                focus_id=request.focus_id,
                session_id=request.session_id,
                payload={
                    "kind": "session_final",
                    "job_id": request.job_id,
                    "session_id": request.session_id,
                    "source": request.source.value,
                    "focus_id": request.focus_id,
                    "text": snap.text,
                    "language": snap.language or "",
                },
            )

    def submit_from_audio(
        self,
        request: UtteranceFromAudio,
        audio: bytes,
        *,
        focus_id: str = "",
    ) -> JobRecord:
        if not request.job_id:
            raise LogicError(ErrorCode.invalid_request, "job_id 必传")
        if not audio:
            raise LogicError(ErrorCode.invalid_request, "音频不能为空")
        if self._transcribe is None:
            raise LogicError(ErrorCode.asr_unavailable, "未注入转写能力，无法 from-audio")
        # 只把整段交给转写能力，业务层不做窗口拼接或 suffix/prefix 合并。
        audio_digest = hashlib.sha256(audio).hexdigest()
        stt = self._transcribe(audio, language=request.language, context=request.context)
        if not stt.text.strip():
            raise LogicError(ErrorCode.invalid_request, "转写结果为空，不入队")
        language = stt.language or request.language or ""
        return self._commit(
            job_id=request.job_id,
            raw_text=stt.text,
            language=language,
            source=UtteranceSource.web,
            focus_id=focus_id,
            payload={
                "kind": "from_audio",
                "job_id": request.job_id,
                "source": UtteranceSource.web.value,
                "focus_id": focus_id,
                "language": request.language or "",
                "audio_sha256": audio_digest,
            },
        )

    def submit_resend(
        self,
        request: UtteranceResendRequest,
        *,
        focus_id: str | None = None,
    ) -> UtteranceResendQueued | UtteranceResendNeedsConfirmation:
        if not request.job_id:
            raise LogicError(ErrorCode.invalid_request, "job_id 必传")
        if not request.text.strip():
            raise LogicError(ErrorCode.invalid_request, "重发文本不能为空")
        if not (request.original_job_id or "").strip():
            raise LogicError(
                ErrorCode.invalid_request,
                "resend 必须显式提供 original_job_id，不能省略或扫描同文猜测",
            )
        inherited_focus = focus_id or ""
        original = self._jobs.get(request.original_job_id)
        if isinstance(original, JobQueryUnknown) or original.found is False:
            raise LogicError(
                ErrorCode.not_found,
                f"原 job 不存在: {request.original_job_id}",
            )
        record = original.job
        if focus_id is None:
            focus_id = request.focus_id
        inherited_focus = focus_id if focus_id is not None else record.focus_id
        if record.state == JobState.delivery_unknown and not request.confirm:
            return UtteranceResendNeedsConfirmation(
                original_job_id=record.job_id,
                new_job_id=request.job_id,
            )
        if record.state in (JobState.queued, JobState.claimed):
            raise LogicError(
                ErrorCode.conflict,
                "原 job 仍在队列或投递中，不能重发",
                details={"original_job_id": record.job_id, "state": record.state},
            )
        queued = self._commit(
            job_id=request.job_id,
            raw_text=request.text,
            language="",
            source=UtteranceSource.resend,
            focus_id=inherited_focus,
            original_job_id=request.original_job_id,
            payload={
                "kind": "resend",
                "job_id": request.job_id,
                "source": UtteranceSource.resend.value,
                "focus_id": inherited_focus,
                "text": request.text,
                "original_job_id": request.original_job_id or "",
            },
        )
        return UtteranceResendQueued(job_id=queued.job_id, text=queued.text)

    def _commit(
        self,
        *,
        job_id: str,
        raw_text: str,
        language: str,
        source: UtteranceSource,
        focus_id: str,
        payload: dict[str, object],
        original_job_id: str | None = None,
        session_id: str | None = None,
    ) -> JobRecord:
        if not job_id:
            raise LogicError(ErrorCode.invalid_request, "job_id 必传")
        digest = compute_payload_hash(payload)
        with self._lock_for(job_id):
            existing = self._jobs.get(job_id)
            if getattr(existing, "found", False):
                record = existing.job
                self._reject_or_return(record, digest)
                return record
            prior = self._history.record_for_job(job_id)
            if prior is not None:
                prior_hash = prior.model_dump().get("payload_hash")
                if isinstance(prior_hash, str) and prior_hash != digest:
                    raise LogicError(
                        ErrorCode.conflict,
                        "同 job_id 但 payload 不同",
                        details={"job_id": job_id},
                    )
                # 历史不是投递权威：无法区分「入队尚未写成」与「已输入后 jobs 丢失」，
                # 故缺 job 行时可见拒绝，不从处理记录构造 queued/failed/delivery_unknown，也不再跑 polish。
                raise LogicError(
                    ErrorCode.conflict,
                    "处理历史存在但任务状态缺失，无法确认是否已输入；禁止自动重投",
                    details={"job_id": job_id},
                )
            else:
                cfg = self._config.load()
                final_text, polish_flag, polish_error = self._run_polish(raw_text, language, cfg)
                now = _iso(self._clock())
                history = _history_with_meta(
                    HistoryRecord(
                        timestamp=now,
                        raw_text=raw_text,
                        final_text=final_text,
                        language=language,
                        source=source,
                        polish=polish_flag,
                        job_id=job_id,
                    ),
                    payload_hash=digest,
                    session_id=session_id,
                )
                try:
                    self._history.append_for_job(history)
                except LogicError as exc:
                    failed = _with_hash(
                        self._failed_record(
                            job_id=job_id,
                            text=final_text,
                            source=source,
                            focus_id=focus_id,
                            original_job_id=original_job_id,
                            session_id=session_id,
                            now=now,
                            detail=f"history write failed: {exc.message}",
                        ),
                        digest,
                    )
                    try:
                        self._jobs.enqueue(failed)
                    except LogicError as enqueue_exc:
                        if enqueue_exc.code != ErrorCode.conflict:
                            raise
                    self._events.publish(
                        ProductHistoryWriteFailedEvent(
                            job_id=job_id,
                            message=exc.message,
                            error=exc.code,
                        )
                    )
                    raise LogicError(
                        ErrorCode.history_io,
                        exc.message,
                        exc.details,
                    ) from exc
            if polish_error is not None:
                failed = _with_hash(
                    self._failed_record(
                        job_id=job_id,
                        text=raw_text,
                        source=source,
                        focus_id=focus_id,
                        original_job_id=original_job_id,
                        session_id=session_id,
                        now=now,
                        detail=f"polish failed: {polish_error.message}",
                    ),
                    digest,
                )
                self._jobs.enqueue(failed)
                self._events.publish(
                    ProductErrorEvent(error=polish_error.code, message=polish_error.message)
                )
                raise LogicError(
                    ErrorCode.polish_failed,
                    polish_error.message,
                    polish_error.details,
                ) from polish_error
            record = _with_hash(
                JobRecord(
                    job_id=job_id,
                    state=JobState.queued,
                    text=final_text,
                    source=source,
                    focus_id=focus_id,
                    session_id=session_id,
                    original_job_id=original_job_id,
                    created_at=now,
                    updated_at=now,
                ),
                digest,
            )
            try:
                return self._jobs.enqueue(record)
            except LogicError as exc:
                if exc.code == ErrorCode.conflict:
                    again = self._jobs.get(job_id)
                    if getattr(again, "found", False):
                        self._reject_or_return(again.job, digest)
                        return again.job
                raise

    def _reject_or_return(self, record: JobRecord, digest: str) -> None:
        existing_hash = _record_hash(record)
        if existing_hash != digest:
            raise LogicError(
                ErrorCode.conflict,
                "同 job_id 但 payload 不同",
                details={"job_id": record.job_id},
            )
        if record.state == JobState.failed:
            detail = record.detail or "job 已失败，未入队"
            if detail.startswith("polish failed"):
                code = ErrorCode.polish_failed
            elif detail.startswith("history write failed"):
                code = ErrorCode.history_io
            else:
                code = ErrorCode.invalid_request
            raise LogicError(
                code,
                detail,
                details={"job_id": record.job_id, "state": record.state},
            )

    def _run_polish(
        self,
        raw_text: str,
        language: str,
        cfg: AppConfig,
    ) -> tuple[str, HistoryPolish, LogicError | None]:
        if not cfg.polish_enabled:
            return raw_text, HistoryPolish.off, None
        if self._polish is None:
            return raw_text, HistoryPolish.failed, LogicError(
                ErrorCode.polish_failed, "已启用纠错但未注入纠错能力"
            )
        self._events.publish(ProductPolishingEvent())
        try:
            result = self._polish(
                PolishRequest(
                    text=raw_text,
                    keywords=list(cfg.custom_keywords) or None,
                    language=language or None,
                ),
                model=cfg.ollama_model,
            )
        except LogicError as exc:
            return raw_text, HistoryPolish.failed, exc
        return result.text, HistoryPolish.on, None

    def _failed_record(
        self,
        *,
        job_id: str,
        text: str,
        source: UtteranceSource,
        focus_id: str,
        original_job_id: str | None,
        now: str,
        detail: str,
        session_id: str | None = None,
    ) -> JobRecord:
        return JobRecord(
            job_id=job_id,
            state=JobState.failed,
            text=text,
            source=source,
            focus_id=focus_id,
            session_id=session_id,
            original_job_id=original_job_id,
            created_at=now,
            updated_at=now,
            detail=detail,
        )
