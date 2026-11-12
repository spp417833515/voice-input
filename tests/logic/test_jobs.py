from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from _contracts import ErrorCode, JobRecord, JobState, LogicError
from _jobs import JobStore


def _rec(job_id: str, focus_id: str, text: str = "t", state: JobState = JobState.queued) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        state=state,
        text=text,
        focus_id=focus_id,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


def test_put_get_and_per_job_focus(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    store.put(_rec("j1", "focus-a"))
    store.put(_rec("j2", "focus-b"))
    assert store.get("j1").focus_id == "focus-a"
    assert store.get("j2").focus_id == "focus-b"
    assert store.get("missing") is None


def test_update_same_claim_late_ack_is_storage_only(tmp_path: Path):
    """L1 只保存字段，不实现重投。晚到 ack 由调用方再次 update 收敛。"""
    store = JobStore(tmp_path / "jobs.json")
    store.put(
        _rec("j1", "f", state=JobState.delivery_unknown).model_copy(
            update={"claim_id": "c1", "detail": "ack lost"}
        )
    )
    updated = store.update("j1", state=JobState.done, detail="late ack complete")
    assert updated.state == JobState.done
    assert updated.claim_id == "c1"
    assert store.get("j1").state == JobState.done


def test_update_missing_and_invalid_state(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    with pytest.raises(LogicError) as ei:
        store.update("nope", state=JobState.done)
    assert ei.value.code.value == "not_found"
    store.put(_rec("j1", "f"))
    with pytest.raises(LogicError) as ei2:
        store.update("j1", state="running")
    assert ei2.value.code.value == "invalid_request"


def test_corrupt_file_is_visible(tmp_path: Path):
    path = tmp_path / "jobs.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(LogicError) as ei:
        JobStore(path).load()
    assert ei.value.code.value == "jobs_io"
    assert "损坏" in ei.value.message or "JSON" in ei.value.message or "schema" in ei.value.message


def test_concurrent_puts_are_not_lost(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")

    def write(i: int) -> None:
        store.put(_rec(f"job-{i}", f"focus-{i}", text=str(i)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(20)))
    jobs = {j.job_id: j for j in store.list_jobs()}
    assert len(jobs) == 20
    for i in range(20):
        assert jobs[f"job-{i}"].focus_id == f"focus-{i}"
        assert jobs[f"job-{i}"].text == str(i)
    raw = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1


def test_compare_and_update_only_one_winner(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    store.put(_rec("j1", "focus-a", state=JobState.queued))
    barrier = threading.Barrier(2)
    winners: list[str] = []
    conflicts: list[str] = []
    lock = threading.Lock()

    def attempt(claim_id: str) -> None:
        barrier.wait()
        try:
            updated = store.update_if(
                "j1",
                expected={"state": JobState.queued},
                fields={"state": JobState.claimed, "claim_id": claim_id},
            )
            with lock:
                winners.append(updated.claim_id or "")
        except LogicError as exc:
            assert exc.code == ErrorCode.conflict
            with lock:
                conflicts.append(claim_id)

    t1 = threading.Thread(target=attempt, args=("c-left",))
    t2 = threading.Thread(target=attempt, args=("c-right",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert len(winners) == 1
    assert len(conflicts) == 1
    current = store.get("j1")
    assert current is not None
    assert current.state == JobState.claimed
    assert current.claim_id == winners[0]


def test_update_if_missing_and_mismatched_condition(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    with pytest.raises(LogicError) as ei:
        store.update_if("nope", expected={"state": JobState.queued}, fields={"state": JobState.claimed})
    assert ei.value.code == ErrorCode.not_found
    store.put(_rec("j1", "f", state=JobState.claimed))
    with pytest.raises(LogicError) as ei2:
        store.update_if("j1", expected={"state": JobState.queued}, fields={"state": JobState.done})
    assert ei2.value.code == ErrorCode.conflict
    assert store.get("j1").state == JobState.claimed


def test_put_if_absent_writes_once_and_conflicts_without_overwrite(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    first = store.put_if_absent(_rec("j1", "focus-a", text="keep"))
    assert first.text == "keep"
    with pytest.raises(LogicError) as ei:
        store.put_if_absent(_rec("j1", "focus-b", text="overwrite"))
    assert ei.value.code.value == "conflict"
    assert store.get("j1").text == "keep"
    assert store.get("j1").focus_id == "focus-a"
    missing = JobStore(tmp_path / "missing-dir" / "jobs.json")
    created = missing.put_if_absent(_rec("j2", "f2", text="new"))
    assert created.job_id == "j2"
    assert missing.get("j2").text == "new"
