from __future__ import annotations

from pathlib import Path

import pytest

from _contracts import HistoryPolish, HistoryRecord, LogicError, UtteranceSource
from _history import HistoryStore

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def test_compatible_old_jsonl_and_skips_malformed(tmp_path: Path):
    src = FIXTURE_DIR / "history_old.jsonl"
    dest = tmp_path / "history.jsonl"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    result = HistoryStore(dest).list_records(limit=10, offset=0)
    assert result.total == 2
    assert result.skipped_malformed == 1
    # 最新在前
    assert result.records[0].final_text == "第二句"
    assert result.records[1].raw_text == "第一句"
    assert result.records[0].mode == "raw"


def test_append_then_list(tmp_path: Path):
    path = tmp_path / "history.jsonl"
    store = HistoryStore(path)
    store.append(
        HistoryRecord(
            timestamp="2026-01-01T00:00:00",
            raw_text="a",
            final_text="a",
            mode="raw",
            language="zh",
        )
    )
    store.append(
        HistoryRecord(
            timestamp="2026-01-01T00:00:01",
            raw_text="b",
            final_text="B",
            mode="polished",
            language="zh",
        )
    )
    listed = store.list_records(limit=1, offset=0)
    assert listed.total == 2
    assert listed.records[0].final_text == "B"
    page2 = store.list_records(limit=1, offset=1)
    assert page2.records[0].raw_text == "a"


def test_missing_file_is_empty(tmp_path: Path):
    result = HistoryStore(tmp_path / "nope.jsonl").list_records()
    assert result.total == 0
    assert result.records == []


def test_unreadable_file_is_visible(tmp_path: Path):
    path = tmp_path / "history.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        with pytest.raises(LogicError) as ei:
            HistoryStore(path).list_records()
        assert ei.value.code.value == "history_io"
    finally:
        path.chmod(0o600)


def test_negative_limit_rejected(tmp_path: Path):
    with pytest.raises(LogicError) as ei:
        HistoryStore(tmp_path / "h.jsonl").list_records(limit=-1)
    assert ei.value.code.value == "invalid_request"


def test_append_new_fields_roundtrip(tmp_path: Path):
    path = tmp_path / "history.jsonl"
    store = HistoryStore(path)
    store.append(
        HistoryRecord(
            timestamp="2026-01-01T00:00:02",
            raw_text="c",
            final_text="C",
            language="zh",
            source=UtteranceSource.resend,
            polish=HistoryPolish.on,
            job_id="job-c",
        )
    )
    listed = store.list_records()
    rec = listed.records[0]
    assert rec.source == UtteranceSource.resend
    assert rec.polish == HistoryPolish.on
    assert rec.job_id == "job-c"
    assert rec.mode is None
