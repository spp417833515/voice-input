"""隔离集成用的显式夹具。不是官方 ASR/Ollama，不得当默认生产。"""
from __future__ import annotations

from _contracts import (
    RecognizeSessionCreate,
    SessionFinalEvent,
    SessionPartialEvent,
    SttResponse,
)


class FixtureStreamProvider:
    official_engine = False

    def __init__(self, final_text: str = "fixture-final", language: str = "zh") -> None:
        self.final_text = final_text
        self.language = language
        self.started: dict[str, RecognizeSessionCreate] = {}
        self.chunks: dict[str, list[int]] = {}
        self.cancelled: set[str] = set()

    def start(self, session_id: str, body: RecognizeSessionCreate, *, sink=None) -> None:
        del sink
        self.started[session_id] = body
        self.chunks[session_id] = []

    def push_pcm(self, session_id: str, index: int, pcm: bytes) -> SessionPartialEvent:
        del pcm
        self.chunks.setdefault(session_id, []).append(index)
        return SessionPartialEvent(
            session_id=session_id,
            stable_text="",
            tail_text="fixture-tail",
            language=self.language,
            audio_end_s=0.5 * (index + 1),
        )

    def finish(self, session_id: str) -> SessionFinalEvent:
        return SessionFinalEvent(session_id=session_id, text=self.final_text, language=self.language)

    def cancel(self, session_id: str) -> None:
        self.cancelled.add(session_id)


def fixture_transcribe(audio: bytes, *, language: str | None = None, context: str | None = None) -> SttResponse:
    del context
    try:
        text = audio.decode("utf-8")
        if text and text.isprintable() and "\x00" not in text:
            if text.startswith("FAIL:"):
                from _contracts import ErrorCode, LogicError

                raise LogicError(ErrorCode.asr_unavailable, "fixture transcribe failed")
            return SttResponse(text=text, language=language or "zh")
    except UnicodeDecodeError:
        pass
    return SttResponse(text="fixture-transcript", language=language or "zh")
