"""业务层测试夹具。禁止读写 live 配置/历史/jobs，禁止注入真实窗口或启动正式 daemon。"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from _config import ConfigStore
from _contracts import AppConfig, PolishRequest, PolishResponse, SttResponse
from _events import EventBus
from _history import HistoryStore
from _history_service import HistoryService
from _jobs import JobStore
from _jobs_service import JobsService
from _session_finals import SessionFinalIndex
from _utterance import UtteranceService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE_CONFIG = PROJECT_ROOT / ".config.json"
LIVE_HISTORY = PROJECT_ROOT / ".cache" / "history.jsonl"
LIVE_JOBS = PROJECT_ROOT / ".cache" / "jobs.json"


def _fingerprint(path: Path) -> tuple[bool, int | None, int | None]:
    if not path.exists():
        return (False, None, None)
    st = path.stat()
    return (True, st.st_mtime_ns, st.st_size)


@pytest.fixture(scope="session")
def live_fingerprints() -> dict[str, tuple[bool, int | None, int | None]]:
    return {
        "config": _fingerprint(LIVE_CONFIG),
        "history": _fingerprint(LIVE_HISTORY),
        "jobs": _fingerprint(LIVE_JOBS),
    }


@pytest.fixture(autouse=True, scope="session")
def _forbid_live_mutation(live_fingerprints):
    yield
    after = {
        "config": _fingerprint(LIVE_CONFIG),
        "history": _fingerprint(LIVE_HISTORY),
        "jobs": _fingerprint(LIVE_JOBS),
    }
    assert after == live_fingerprints, f"业务测试不得改动 live 文件: {after}"


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.events_log: list[object] = []
        self.bus = EventBus()
        self.bus.subscribe(self.events_log.append)
        self.config = ConfigStore(tmp_path / "config.json")
        self.config.save(AppConfig(ollama_model="qwen3.5:4b", polish_enabled=False))
        self.history = HistoryService(HistoryStore(tmp_path / "history.jsonl"), environ={})
        self.jobs_store = JobStore(tmp_path / "jobs.json")
        self.jobs = JobsService(self.jobs_store, self.bus, claim_lease_s=60.0)
        self.finals = SessionFinalIndex()
        self.polish_calls = 0
        self.transcribe_calls = 0
        self.service = UtteranceService(
            jobs=self.jobs,
            history=self.history,
            events=self.bus,
            config=self.config,
            finals=self.finals,
            polish=self.polish,
            transcribe=self.transcribe,
        )

    def polish(self, request: PolishRequest, *, model: str) -> PolishResponse:
        self.polish_calls += 1
        if getattr(self, "polish_impl", None) is not None:
            return self.polish_impl(request, model=model)
        return PolishResponse(text=request.text.replace("错字", "正字"))

    def transcribe(self, audio: bytes, *, language: str | None = None, context: str | None = None) -> SttResponse:
        self.transcribe_calls += 1
        if getattr(self, "transcribe_impl", None) is not None:
            return self.transcribe_impl(audio, language=language, context=context)
        return SttResponse(text=audio.decode("utf-8"), language=language or "zh")

    def event_types(self) -> list[str]:
        return [getattr(item, "event", None) for item in self.events_log]


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)
