from __future__ import annotations

import pytest

from _contracts import (
    ErrorCode,
    HistoryRecord,
    LogicError,
    ProductIdleEvent,
    ProductPartialEvent,
    UtteranceSource,
)
from _events import EventBus
from _history import HistoryStore
from _history_service import HistoryService, detect_multi_worker
from tests.business.conftest import Harness
from tests.business.test_utterance import _final, _req


def test_detect_and_reject_multi_worker(tmp_path):
    assert detect_multi_worker({}) is None
    assert detect_multi_worker({"WEB_CONCURRENCY": "1"}) is None
    assert detect_multi_worker({"WEB_CONCURRENCY": "2"}) == "WEB_CONCURRENCY=2"
    with pytest.raises(LogicError) as ei:
        HistoryService(HistoryStore(tmp_path / "h.jsonl"), environ={"UVICORN_WORKERS": "4"})
    assert ei.value.code == ErrorCode.internal
    assert "UVICORN_WORKERS=4" in ei.value.message
    with pytest.raises(LogicError) as ei2:
        HistoryService(HistoryStore(tmp_path / "h.jsonl"), environ={}, worker_count=3)
    assert "worker_count=3" in ei2.value.message


def test_history_is_processing_record_not_delivery(harness: Harness):
    _final(harness, "s1", "处理文本")
    job = harness.service.submit_session_final(_req("job-h", "s1"))
    rec = harness.history.list_records().records[0]
    assert rec.final_text == "处理文本"
    assert rec.final_text == job.text
    # 入队不等于已输入
    assert job.state != "done"
    assert rec.job_id == job.job_id


def test_event_bus_accepts_frozen_schema_only():
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe(seen.append)
    bus.publish(ProductIdleEvent())
    bus.publish(ProductPartialEvent(stable_text="稳", tail_text="尾"))
    assert seen[0].event == "idle"
    assert seen[1].stable_text == "稳"
    with pytest.raises(Exception):
        bus.publish({"event": "status", "style": "normal", "text": "x"})  # type: ignore[arg-type]


def test_serial_history_appends_visible(tmp_path):
    svc = HistoryService(HistoryStore(tmp_path / "h.jsonl"), environ={})
    svc.append(
        HistoryRecord(
            timestamp="t1",
            raw_text="a",
            final_text="A",
            language="zh",
            source=UtteranceSource.desktop,
            job_id="j1",
        )
    )
    svc.append(
        HistoryRecord(
            timestamp="t2",
            raw_text="b",
            final_text="B",
            language="zh",
            source=UtteranceSource.web,
            job_id="j2",
        )
    )
    listed = svc.list_records()
    assert listed.total == 2
    assert listed.records[0].final_text == "B"


def test_append_for_job_is_idempotent_and_legacy_without_job_id(tmp_path):
    svc = HistoryService(HistoryStore(tmp_path / "h.jsonl"), environ={})
    first = HistoryRecord(
        timestamp="t1",
        raw_text="a",
        final_text="A",
        language="zh",
        source=UtteranceSource.desktop,
        job_id="j1",
    )
    svc.append_for_job(first)
    svc.append_for_job(
        HistoryRecord(
            timestamp="t1-again",
            raw_text="a",
            final_text="A",
            language="zh",
            source=UtteranceSource.desktop,
            job_id="j1",
        )
    )
    assert svc.list_records().total == 1
    assert svc.record_for_job("j1") is not None
    # 旧记录没有 job_id：仍每次追加，不误判为命中
    legacy = HistoryRecord(
        timestamp="old",
        raw_text="legacy",
        final_text="legacy",
        language="zh",
        mode="raw",
    )
    svc.append_for_job(legacy)
    svc.append_for_job(legacy)
    assert svc.list_records().total == 3
    assert svc.record_for_job("") is None
