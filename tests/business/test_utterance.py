from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from _contracts import (
    AppConfig,
    ErrorCode,
    AckResult,
    HistoryPolish,
    JobAckRequest,
    JobState,
    LogicError,
    PolishRequest,
    PolishResponse,
    SessionClosedReason,
    UtteranceCreate,
    UtteranceFromAudio,
    UtteranceResendNeedsConfirmation,
    UtteranceResendQueued,
    UtteranceResendRequest,
    UtteranceSource,
)
from tests.business.conftest import Harness


def _final(h: Harness, session_id: str, text: str, language: str = "zh") -> None:
    h.finals.record_final(session_id, text, language)


def _req(job_id: str, session_id: str, focus_id: str = "focus-a") -> UtteranceCreate:
    return UtteranceCreate(
        job_id=job_id,
        session_id=session_id,
        source=UtteranceSource.desktop,
        focus_id=focus_id,
    )


def test_desktop_final_queues_after_history_without_polish(harness: Harness):
    _final(harness, "s1", "你好世界")
    job = harness.service.submit_session_final(_req("job-1", "s1"))
    assert job.state == JobState.queued
    assert job.text == "你好世界"
    assert job.focus_id == "focus-a"
    assert job.session_id == "s1"
    records = harness.history.list_records().records
    assert len(records) == 1
    rec = records[0]
    assert rec.raw_text == "你好世界"
    assert rec.final_text == "你好世界"
    assert rec.final_text != "已输入成功"
    assert rec.polish == HistoryPolish.off
    assert rec.source == UtteranceSource.desktop
    assert rec.job_id == "job-1"
    assert harness.jobs_store.get("job-1") is not None
    assert "payload_hash" in job.model_dump()
    assert "audio" not in job.model_dump() or job.model_dump().get("audio") in (None, "")


def test_polish_success_then_history_then_queue(harness: Harness):
    harness.config.save(AppConfig(ollama_model="mock-4b", polish_enabled=True))
    _final(harness, "s1", "这是错字")
    job = harness.service.submit_session_final(_req("job-p", "s1"))
    assert job.text == "这是正字"
    rec = harness.history.list_records().records[0]
    assert rec.raw_text == "这是错字"
    assert rec.final_text == "这是正字"
    assert rec.polish == HistoryPolish.on
    assert job.state == JobState.queued
    assert "polishing" in harness.event_types()
    assert harness.polish_calls == 1


def test_polish_failure_records_raw_and_does_not_queue(harness: Harness):
    harness.config.save(AppConfig(ollama_model="mock-4b", polish_enabled=True))

    def boom(request: PolishRequest, *, model: str) -> PolishResponse:
        raise LogicError(ErrorCode.polish_failed, "模型超时")

    harness.polish_impl = boom
    _final(harness, "s1", "原文")
    with pytest.raises(LogicError) as ei:
        harness.service.submit_session_final(_req("job-pf", "s1"))
    assert ei.value.code == ErrorCode.polish_failed
    rec = harness.history.list_records().records[0]
    assert rec.raw_text == "原文"
    assert rec.final_text == "原文"
    assert rec.polish == HistoryPolish.failed
    stored = harness.jobs_store.get("job-pf")
    assert stored is not None
    assert stored.state == JobState.failed
    assert stored.state != JobState.queued


def test_history_write_failure_does_not_enqueue(harness: Harness, tmp_path):
    def fail_append(record):
        raise LogicError(ErrorCode.history_io, "disk full")

    harness.history.append_for_job = fail_append  # type: ignore[method-assign]
    _final(harness, "s1", "一句")
    with pytest.raises(LogicError) as ei:
        harness.service.submit_session_final(_req("job-h", "s1"))
    assert ei.value.code == ErrorCode.history_io
    stored = harness.jobs_store.get("job-h")
    assert stored is not None
    assert stored.state == JobState.failed
    assert "history_write_failed" in harness.event_types()


def test_same_job_same_payload_returns_existing_without_second_history(harness: Harness):
    _final(harness, "s1", "同一句")
    first = harness.service.submit_session_final(_req("job-dup", "s1"))
    second = harness.service.submit_session_final(_req("job-dup", "s1"))
    assert first.job_id == second.job_id
    assert first.state == JobState.queued
    assert second.state == JobState.queued
    assert harness.history.list_records().total == 1


def test_same_job_different_payload_conflicts(harness: Harness):
    _final(harness, "s1", "第一句")
    harness.service.submit_session_final(_req("job-x", "s1", "focus-a"))
    _final(harness, "s1", "第一句")
    with pytest.raises(LogicError) as ei:
        harness.service.submit_session_final(_req("job-x", "s1", "focus-b"))
    assert ei.value.code == ErrorCode.conflict
    assert harness.history.list_records().total == 1
    assert harness.jobs_store.get("job-x").focus_id == "focus-a"


def test_missing_final_or_cancel_does_not_queue(harness: Harness):
    with pytest.raises(LogicError) as ei:
        harness.service.submit_session_final(_req("job-no", "missing"))
    assert ei.value.code == ErrorCode.invalid_request
    assert harness.jobs_store.get("job-no") is None
    harness.finals.record_final("s-c", "已被取消")
    harness.finals.record_closed("s-c", SessionClosedReason.cancelled)
    with pytest.raises(LogicError) as ei2:
        harness.service.submit_session_final(_req("job-c", "s-c"))
    assert "取消" in ei2.value.message
    assert harness.jobs_store.get("job-c") is None


def test_watchdog_finish_does_not_cancel_previous_queued(harness: Harness):
    _final(harness, "s1", "第一句")
    first = harness.service.submit_session_final(_req("job-1", "s1", "focus-1"))
    _final(harness, "s2", "第二句")
    second = harness.service.submit_session_final(_req("job-2", "s2", "focus-2"))
    assert first.state == JobState.queued
    assert second.state == JobState.queued
    assert harness.jobs_store.get("job-1").state == JobState.queued
    assert harness.jobs_store.get("job-1").focus_id == "focus-1"
    assert harness.jobs_store.get("job-2").focus_id == "focus-2"
    assert first.focus_id != second.focus_id


def test_from_audio_shares_pipeline_and_stores_digest_not_bytes(harness: Harness):
    job = harness.service.submit_from_audio(
        UtteranceFromAudio(job_id="job-phone", language="zh"),
        audio="手机一句".encode("utf-8"),
        focus_id="phone-focus",
    )
    assert job.source == UtteranceSource.web
    assert job.text == "手机一句"
    assert job.state == JobState.queued
    dumped = job.model_dump()
    assert "audio" not in dumped
    rec = harness.history.list_records().records[0]
    assert rec.source == UtteranceSource.web
    assert rec.job_id == "job-phone"
    assert harness.transcribe_calls == 1
    with pytest.raises(LogicError) as ei:
        harness.service.submit_from_audio(UtteranceFromAudio(job_id="job-empty"), audio=b"")
    assert ei.value.code == ErrorCode.invalid_request


def test_from_audio_same_job_different_audio_conflicts(harness: Harness):
    harness.service.submit_from_audio(
        UtteranceFromAudio(job_id="job-a", language="zh"),
        audio=b"aaa",
    )
    with pytest.raises(LogicError) as ei:
        harness.service.submit_from_audio(
            UtteranceFromAudio(job_id="job-a", language="zh"),
            audio=b"bbb",
        )
    assert ei.value.code == ErrorCode.conflict


def test_resend_delivery_unknown_requires_confirmation(harness: Harness):
    _final(harness, "s1", "可能已输入")
    original = harness.service.submit_session_final(_req("job-old", "s1"))
    harness.jobs_store.update("job-old", state=JobState.delivery_unknown, claim_id="c1")
    result = harness.service.submit_resend(
        UtteranceResendRequest(job_id="job-new", text=original.text, original_job_id="job-old")
    )
    assert isinstance(result, UtteranceResendNeedsConfirmation)
    assert result.ok is False
    assert result.requires_confirmation is True
    assert result.original_job_id == "job-old"
    assert result.new_job_id == "job-new"
    assert harness.jobs_store.get("job-new") is None
    assert harness.history.list_records().total == 1
    confirmed = harness.service.submit_resend(
        UtteranceResendRequest(
            job_id="job-new",
            text=original.text,
            original_job_id="job-old",
            confirm=True,
        )
    )
    assert isinstance(confirmed, UtteranceResendQueued)
    assert confirmed.ok is True
    created = harness.jobs_store.get("job-new")
    assert created is not None
    assert created.state == JobState.queued
    assert created.source == UtteranceSource.resend
    assert created.original_job_id == "job-old"
    assert created.focus_id == "focus-a"


def test_resend_failed_does_not_need_confirmation(harness: Harness):
    _final(harness, "s1", "失败原文")
    harness.service.submit_session_final(_req("job-fail", "s1"))
    harness.jobs_store.update("job-fail", state=JobState.failed, detail="ack none")
    result = harness.service.submit_resend(
        UtteranceResendRequest(job_id="job-retry", text="失败原文", original_job_id="job-fail")
    )
    assert isinstance(result, UtteranceResendQueued)
    assert harness.jobs_store.get("job-retry").state == JobState.queued
    assert harness.jobs_store.get("job-retry").original_job_id == "job-fail"


def test_resend_missing_original_or_empty_text(harness: Harness):
    with pytest.raises(LogicError) as ei:
        harness.service.submit_resend(
            UtteranceResendRequest(job_id="n", text="x", original_job_id="nope")
        )
    assert ei.value.code == ErrorCode.not_found
    with pytest.raises(LogicError) as ei2:
        harness.service.submit_resend(UtteranceResendRequest(job_id="n", text="   ", original_job_id="x"))
    assert ei2.value.code == ErrorCode.invalid_request


def test_concurrent_same_job_only_one_history_and_queue(harness: Harness):
    _final(harness, "s1", "并发一句")
    errors: list[BaseException] = []
    results: list[object] = []
    barrier = threading.Barrier(8)

    def run():
        try:
            barrier.wait()
            results.append(harness.service.submit_session_final(_req("job-race", "s1")))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: run(), range(8)))
    assert errors == []
    assert len(results) == 8
    assert harness.history.list_records().total == 1
    assert harness.jobs_store.get("job-race").state == JobState.queued


def test_same_session_second_job_id_conflicts(harness: Harness):
    _final(harness, "s1", "同一句定稿")
    first = harness.service.submit_session_final(_req("job-a", "s1", "fa"))
    assert first.session_id == "s1"
    with pytest.raises(LogicError) as ei:
        harness.service.submit_session_final(_req("job-b", "s1", "fb"))
    assert ei.value.code == ErrorCode.conflict
    assert harness.jobs_store.get("job-b") is None
    assert harness.history.list_records().total == 1
    again = harness.service.submit_session_final(_req("job-a", "s1", "fa"))
    assert again.job_id == "job-a"
    assert again.state == JobState.queued
    assert harness.history.list_records().total == 1


def test_session_bind_survives_fresh_index_after_restart(harness: Harness):
    _final(harness, "s1", "同一句定稿")
    harness.service.submit_session_final(_req("job-a", "s1", "fa"))
    restarted = Harness(harness.tmp)
    _final(restarted, "s1", "同一句定稿")
    with pytest.raises(LogicError) as ei:
        restarted.service.submit_session_final(_req("job-b", "s1", "fb"))
    assert ei.value.code == ErrorCode.conflict
    assert restarted.jobs_store.get("job-a").session_id == "s1"
    assert restarted.jobs_store.get("job-b") is None
    same = restarted.service.submit_session_final(_req("job-a", "s1", "fa"))
    assert same.job_id == "job-a"
    assert restarted.history.list_records().total == 1


def test_concurrent_same_session_only_one_job_accepted(harness: Harness):
    _final(harness, "s1", "同一句定稿")
    barrier = threading.Barrier(8)
    accepted: list[str] = []
    conflicts: list[str] = []
    lock = threading.Lock()

    def attempt(i: int) -> None:
        barrier.wait()
        try:
            rec = harness.service.submit_session_final(_req(f"job-{i}", "s1", f"f-{i}"))
            with lock:
                accepted.append(rec.job_id)
        except LogicError as exc:
            assert exc.code == ErrorCode.conflict
            with lock:
                conflicts.append(f"job-{i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, range(8)))
    assert len(accepted) == 1
    assert len(conflicts) == 7
    assert harness.history.list_records().total == 1
    winner = harness.jobs_store.get(accepted[0])
    assert winner is not None
    assert winner.session_id == "s1"
    assert winner.state == JobState.queued


def test_resend_without_original_job_id_is_rejected(harness: Harness):
    _final(harness, "s1", "可能已输入")
    harness.service.submit_session_final(_req("job-old", "s1"))
    harness.jobs_store.update("job-old", state=JobState.delivery_unknown, claim_id="c1")
    with pytest.raises(ValidationError):
        UtteranceResendRequest(job_id="job-bypass", text="可能已输入")
    assert harness.jobs_store.get("job-bypass") is None
    assert harness.history.list_records().total == 1


def test_confirmed_resend_retry_is_idempotent(harness: Harness):
    _final(harness, "s1", "可能已输入")
    original = harness.service.submit_session_final(_req("job-old", "s1"))
    harness.jobs_store.update("job-old", state=JobState.delivery_unknown, claim_id="c1")
    first = harness.service.submit_resend(
        UtteranceResendRequest(
            job_id="job-new",
            text=original.text,
            original_job_id="job-old",
            confirm=True,
        )
    )
    second = harness.service.submit_resend(
        UtteranceResendRequest(
            job_id="job-new",
            text=original.text,
            original_job_id="job-old",
            confirm=True,
        )
    )
    assert first.ok is True and second.ok is True
    assert first.job_id == second.job_id == "job-new"
    assert harness.history.list_records().total == 2
    assert harness.jobs_store.get("job-new").original_job_id == "job-old"


_HISTORY_WITHOUT_JOB = "处理历史存在但任务状态缺失，无法确认是否已输入；禁止自动重投"


def _wipe_jobs(h: Harness) -> None:
    (h.tmp / "jobs.json").write_text("", encoding="utf-8")


def _assert_history_without_job_rejected(exc: LogicError, harness: Harness, job_id: str) -> None:
    assert exc.code == ErrorCode.conflict
    assert exc.message == _HISTORY_WITHOUT_JOB
    assert harness.history.list_records().total == 1
    assert harness.jobs_store.get(job_id) is None
    assert [j.job_id for j in harness.jobs_store.list_jobs() if j.state == JobState.queued] == []


def test_history_written_enqueue_failure_retry_does_not_duplicate(harness: Harness):
    _final(harness, "s1", "一句")
    real_enqueue = harness.jobs.enqueue

    def fail_enqueue(record):
        raise LogicError(ErrorCode.jobs_io, "disk full")

    harness.jobs.enqueue = fail_enqueue  # type: ignore[method-assign]
    with pytest.raises(LogicError) as ei:
        harness.service.submit_session_final(_req("job-h", "s1"))
    assert ei.value.code == ErrorCode.jobs_io
    assert harness.history.list_records().total == 1
    assert harness.jobs_store.get("job-h") is None
    harness.jobs.enqueue = real_enqueue  # type: ignore[method-assign]
    # jobs 尚无记录时，不同 payload 仍 409，且不得再写历史或入队
    with pytest.raises(LogicError) as ei2:
        harness.service.submit_session_final(_req("job-h", "s1", "other-focus"))
    assert ei2.value.code == ErrorCode.conflict
    assert ei2.value.message == "同 job_id 但 payload 不同"
    assert harness.history.list_records().total == 1
    assert harness.jobs_store.get("job-h") is None
    # 安全取舍：历史已写而任务状态缺失时，同请求也不得补投
    with pytest.raises(LogicError) as ei3:
        harness.service.submit_session_final(_req("job-h", "s1"))
    _assert_history_without_job_rejected(ei3.value, harness, "job-h")
    # 模拟崩溃后新索引、同一 store：仍拒绝，不从历史建队
    restarted = Harness(harness.tmp)
    _final(restarted, "s1", "一句")
    with pytest.raises(LogicError) as ei4:
        restarted.service.submit_session_final(_req("job-h", "s1"))
    _assert_history_without_job_rejected(ei4.value, restarted, "job-h")
    assert restarted.jobs.recover() == []
    assert restarted.jobs_store.list_jobs() == []


def test_submit_after_done_and_jobs_file_emptied_is_rejected(harness: Harness):
    _final(harness, "s1", "一句")
    queued = harness.service.submit_session_final(_req("job-done", "s1"))
    assert queued.state == JobState.queued
    claimed = harness.jobs.claim()
    assert claimed is not None
    harness.jobs.ack(
        "job-done",
        JobAckRequest(claim_id=claimed.claim_id, result=AckResult.complete),
    )
    assert harness.jobs_store.get("job-done").state == JobState.done
    _wipe_jobs(harness)
    assert harness.jobs.recover() == []
    assert harness.jobs_store.list_jobs() == []
    restarted = Harness(harness.tmp)
    _final(restarted, "s1", "一句")
    with pytest.raises(LogicError) as ei:
        restarted.service.submit_session_final(_req("job-done", "s1"))
    _assert_history_without_job_rejected(ei.value, restarted, "job-done")
    with pytest.raises(LogicError) as other:
        restarted.service.submit_session_final(_req("job-other", "s1"))
    assert other.value.code == ErrorCode.conflict
    assert restarted.history.list_records().total == 1
    assert restarted.jobs_store.list_jobs() == []


def test_submit_after_delivery_unknown_and_jobs_file_emptied_is_rejected(harness: Harness):
    _final(harness, "s1", "一句")
    harness.service.submit_session_final(_req("job-unk", "s1"))
    claimed = harness.jobs.claim()
    assert claimed is not None
    harness.jobs.ack(
        "job-unk",
        JobAckRequest(claim_id=claimed.claim_id, result=AckResult.partial_or_unknown),
    )
    assert harness.jobs_store.get("job-unk").state == JobState.delivery_unknown
    _wipe_jobs(harness)
    assert harness.jobs.recover() == []
    restarted = Harness(harness.tmp)
    _final(restarted, "s1", "一句")
    with pytest.raises(LogicError) as ei:
        restarted.service.submit_session_final(_req("job-unk", "s1"))
    _assert_history_without_job_rejected(ei.value, restarted, "job-unk")
    assert restarted.jobs.recover() == []
    assert restarted.jobs_store.list_jobs() == []


def test_polish_failed_history_without_job_retry_is_rejected(harness: Harness):
    harness.config.save(AppConfig(ollama_model="mock-4b", polish_enabled=True))

    def boom(request, *, model):
        raise LogicError(ErrorCode.polish_failed, "model down")

    harness.polish_impl = boom  # type: ignore[attr-defined]
    real_enqueue = harness.jobs.enqueue

    def fail_enqueue(record):
        raise LogicError(ErrorCode.jobs_io, "disk full")

    harness.jobs.enqueue = fail_enqueue  # type: ignore[method-assign]
    _final(harness, "s1", "原文")
    with pytest.raises(LogicError) as first:
        harness.service.submit_session_final(_req("job-pf", "s1"))
    assert first.value.code == ErrorCode.jobs_io
    rec = harness.history.list_records().records[0]
    assert rec.job_id == "job-pf"
    assert rec.polish.value == "failed"
    assert harness.jobs_store.get("job-pf") is None
    harness.jobs.enqueue = real_enqueue  # type: ignore[method-assign]
    with pytest.raises(LogicError) as ei:
        harness.service.submit_session_final(_req("job-pf", "s1"))
    _assert_history_without_job_rejected(ei.value, harness, "job-pf")
    assert harness.history.list_records().records[0].polish.value == "failed"
    assert harness.polish_calls == 1


def test_missing_polish_capability_matches_failure_path(harness: Harness):
    from _contracts import AppConfig

    harness.config.save(AppConfig(ollama_model="mock-4b", polish_enabled=True))
    harness.service._polish = None
    _final(harness, "s1", "原文")
    with pytest.raises(LogicError) as ei:
        harness.service.submit_session_final(_req("job-np", "s1"))
    assert ei.value.code == ErrorCode.polish_failed
    rec = harness.history.list_records().records[0]
    assert rec.raw_text == "原文"
    assert rec.final_text == "原文"
    assert rec.polish.value == "failed"
    stored = harness.jobs_store.get("job-np")
    assert stored is not None
    assert stored.state == JobState.failed
    assert stored.state != JobState.queued
