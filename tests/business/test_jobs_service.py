from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from _contracts import (
    AckResult,
    ErrorCode,
    JobAckRequest,
    JobQueryUnknown,
    JobRecord,
    JobState,
    LogicError,
    UtteranceCreate,
    UtteranceSource,
)
from _events import EventBus
from _jobs import JobStore
from _jobs_service import JobsService
from tests.business.conftest import Harness
from tests.business.test_utterance import _final, _req


def _queued(store: JobStore, job_id: str, focus_id: str, text: str = "t") -> JobRecord:
    rec = JobRecord(
        job_id=job_id,
        state=JobState.queued,
        text=text,
        source=UtteranceSource.desktop,
        focus_id=focus_id,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    return store.put_if_absent(rec)


def test_claim_ack_complete_emits_typed(tmp_path: Path):
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe(seen.append)
    store = JobStore(tmp_path / "jobs.json")
    svc = JobsService(store, bus)
    _queued(store, "j1", "f1", "完整")
    claimed = svc.claim()
    assert claimed is not None
    assert claimed.state == JobState.claimed
    assert claimed.focus_id == "f1"
    updated = svc.ack("j1", JobAckRequest(claim_id=claimed.claim_id, result=AckResult.complete))
    assert updated.state == JobState.done
    assert any(getattr(item, "event", None) == "typed" for item in seen)


def test_ack_none_failed_and_partial_or_unknown(tmp_path: Path):
    bus = EventBus()
    store = JobStore(tmp_path / "jobs.json")
    svc = JobsService(store, bus)
    _queued(store, "j-none", "f")
    c1 = svc.claim()
    failed = svc.ack("j-none", JobAckRequest(claim_id=c1.claim_id, result=AckResult.none))
    assert failed.state == JobState.failed
    _queued(store, "j-part", "f")
    c2 = svc.claim()
    unknown = svc.ack(
        "j-part",
        JobAckRequest(claim_id=c2.claim_id, result=AckResult.partial_or_unknown),
    )
    assert unknown.state == JobState.delivery_unknown


def test_late_ack_after_lost_report_converges_without_redelivery(tmp_path: Path):
    bus = EventBus()
    store = JobStore(tmp_path / "jobs.json")
    svc = JobsService(store, bus)
    _queued(store, "j-late", "f", "迟到")
    claimed = svc.claim()
    store.update("j-late", state=JobState.delivery_unknown, detail="ack lost")
    updated = svc.ack(
        "j-late",
        JobAckRequest(claim_id=claimed.claim_id, result=AckResult.complete),
    )
    assert updated.state == JobState.done
    assert store.get("j-late").state == JobState.done
    assert store.get("j-late").claim_id == claimed.claim_id
    # 不得因此再入队
    assert svc.claim() is None


def test_recover_claimed_becomes_delivery_unknown_and_does_not_requeue(tmp_path: Path):
    bus = EventBus()
    store = JobStore(tmp_path / "jobs.json")
    svc = JobsService(store, bus)
    _queued(store, "j-rec", "f", "恢复")
    claimed = svc.claim()
    assert claimed is not None
    # 模拟进程重启：新服务读同一文件
    restarted = JobsService(JobStore(tmp_path / "jobs.json"), bus)
    changed = restarted.recover()
    assert len(changed) == 1
    assert changed[0].state == JobState.delivery_unknown
    again = JobsService(JobStore(tmp_path / "jobs.json"), bus)
    assert again.claim() is None
    assert again.get("j-rec").job.state == JobState.delivery_unknown
    # queued 任务恢复后仍可被第一次领取，不算重投
    _queued(store, "j-wait", "f2")
    first_claim = again.claim()
    assert first_claim is not None
    assert first_claim.job_id == "j-wait"


def test_claim_timeout_marks_delivery_unknown_not_failed(tmp_path: Path):
    bus = EventBus()
    store = JobStore(tmp_path / "jobs.json")
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    svc = JobsService(store, bus, clock=lambda: clock["now"], claim_lease_s=10)
    rec = JobRecord(
        job_id="j-to",
        state=JobState.queued,
        text="超时",
        focus_id="f",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store.put_if_absent(rec)
    claimed = svc.claim()
    assert claimed is not None
    clock["now"] = clock["now"] + timedelta(seconds=11)
    expired = svc.expire_stale_claims()
    assert len(expired) == 1
    assert expired[0].state == JobState.delivery_unknown
    assert store.get("j-to").state != JobState.failed
    assert store.get("j-to").state != JobState.queued


def test_unknown_job_query_is_not_failed(tmp_path: Path):
    svc = JobsService(JobStore(tmp_path / "jobs.json"), EventBus())
    result = svc.get("missing")
    assert isinstance(result, JobQueryUnknown)
    assert result.state == "unknown"
    assert result.found is False


def test_ack_wrong_claim_or_missing(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    svc = JobsService(store, EventBus())
    with pytest.raises(LogicError) as ei:
        svc.ack("nope", JobAckRequest(claim_id="c", result=AckResult.complete))
    assert ei.value.code == ErrorCode.not_found
    _queued(store, "j1", "f")
    claimed = svc.claim()
    with pytest.raises(LogicError) as ei2:
        svc.ack("j1", JobAckRequest(claim_id="other", result=AckResult.complete))
    assert ei2.value.code == ErrorCode.conflict
    done = svc.ack("j1", JobAckRequest(claim_id=claimed.claim_id, result=AckResult.complete))
    assert done.state == JobState.done
    with pytest.raises(LogicError) as ei3:
        svc.ack("j1", JobAckRequest(claim_id=claimed.claim_id, result=AckResult.complete))
    assert ei3.value.code == ErrorCode.conflict


def test_concurrent_claim_only_one_winner(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    svc = JobsService(store, EventBus())
    _queued(store, "only", "f", "x")
    barrier = threading.Barrier(8)
    winners: list[str] = []
    lock = threading.Lock()

    def attempt():
        barrier.wait()
        claimed = svc.claim()
        if claimed is not None:
            with lock:
                winners.append(claimed.claim_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: attempt(), range(8)))
    assert len(winners) == 1
    assert store.get("only").state == JobState.claimed
    assert store.get("only").claim_id == winners[0]


def test_put_if_absent_conflict_and_idempotent_pipeline(harness: Harness):
    _final(harness, "s1", "一句")
    harness.service.submit_session_final(_req("job-1", "s1"))
    with pytest.raises(LogicError) as ei:
        harness.jobs_store.put_if_absent(harness.jobs_store.get("job-1"))
    assert ei.value.code == ErrorCode.conflict


def test_unparsable_claim_timestamp_becomes_delivery_unknown(tmp_path: Path):
    bus = EventBus()
    store = JobStore(tmp_path / "jobs.json")
    svc = JobsService(store, bus, claim_lease_s=60)
    rec = JobRecord(
        job_id="j-bad-ts",
        state=JobState.queued,
        text="x",
        focus_id="f",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store.put_if_absent(rec)
    claimed = svc.claim()
    assert claimed is not None
    store.update("j-bad-ts", created_at="not-a-timestamp", updated_at="also-bad")
    expired = svc.expire_stale_claims()
    assert len(expired) == 1
    assert expired[0].state == JobState.delivery_unknown
    assert "unparsable" in (expired[0].detail or "")
    assert store.get("j-bad-ts").state != JobState.claimed
