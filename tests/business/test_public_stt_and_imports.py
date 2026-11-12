from __future__ import annotations

import ast
from pathlib import Path

import pytest

from _contracts import LogicError, SttResponse
from tests.business.conftest import Harness
from tests.business.test_utterance import _final, _req

ROOT = Path(__file__).resolve().parents[2]
BUSINESS_FILES = [
    "_utterance.py",
    "_jobs_service.py",
    "_events.py",
    "_history_service.py",
    "_session_finals.py",
]


def test_public_stt_has_no_polish_history_or_queue(harness: Harness):
    result = harness.service.transcribe_public("公开转写".encode("utf-8"), language="zh")
    assert isinstance(result, SttResponse)
    assert result.ok is True
    assert result.text == "公开转写"
    assert harness.history.list_records().total == 0
    assert harness.jobs_store.list_jobs() == []
    assert harness.polish_calls == 0
    assert harness.transcribe_calls == 1


def test_public_stt_does_not_touch_existing_jobs(harness: Harness):
    _final(harness, "s1", "已有")
    harness.service.submit_session_final(_req("job-existing", "s1"))
    before = harness.history.list_records().total
    harness.service.transcribe_public(b"hello", language="en")
    assert harness.history.list_records().total == before
    assert harness.jobs_store.get("job-existing").state.value == "queued"


def test_public_stt_empty_audio_rejected(harness: Harness):
    with pytest.raises(LogicError):
        harness.service.transcribe_public(b"")


def test_business_modules_do_not_import_asr_runtimes():
    forbidden = {"torch", "vllm", "faster_whisper", "whisper"}
    found: dict[str, set[str]] = {}
    for name in BUSINESS_FILES:
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        hits: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in forbidden:
                        hits.add(top)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                if top in forbidden:
                    hits.add(top)
        if hits:
            found[name] = hits
    assert found == {}
