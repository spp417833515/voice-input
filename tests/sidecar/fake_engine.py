"""测试用假引擎。不加载官方模型，不占 GPU。"""
from __future__ import annotations

from _contracts import ErrorCode, LogicError, SessionFinalEvent, SessionPartialEvent, SttResponse


class FakeEngine:
    loaded = True

    def __init__(
        self,
        *,
        text: str = "fixture-text",
        language: str = "zh",
        max_session_bytes: int = 160_000,
        partials_per_chunk: int = 1,
        partial_tails: list[str] | None = None,
        early_partials: list[str] | None = None,
    ) -> None:
        self.text = text
        self.language = language
        self.max_session_bytes = max_session_bytes
        self.partials_per_chunk = partials_per_chunk
        self.partial_tails = partial_tails
        self.early_partials = list(early_partials or [])
        self.streams: dict[str, bytearray] = {}
        self.cancelled: list[str] = []
        self.finished: list[str] = []
        self.transcribe_calls = 0

    def transcribe(self, pcm: bytes, *, language: str | None = None, context: str | None = None) -> SttResponse:
        self.transcribe_calls += 1
        try:
            guessed = pcm.decode("utf-8")
            if guessed.isprintable() and guessed.strip():
                return SttResponse(text=guessed, language=language or self.language)
        except UnicodeDecodeError:
            pass
        return SttResponse(text=self.text, language=language or self.language)

    def start_stream(self, session_id: str, *, language: str | None = None, context: str | None = None) -> None:
        if session_id in self.streams:
            raise LogicError(ErrorCode.conflict, "会话已存在")
        self.streams[session_id] = bytearray()

    def on_events_subscribed(self, session_id: str) -> list[SessionPartialEvent]:
        """测试钩子：SSE 一挂上就发出的 early partial。官方引擎没有此方法。"""
        if session_id not in self.streams or not self.early_partials:
            return []
        return [
            SessionPartialEvent(
                session_id=session_id,
                stable_text="",
                tail_text=tail,
                language=self.language,
                audio_end_s=0.0,
            )
            for tail in self.early_partials
        ]

    def push_pcm(self, session_id: str, pcm: bytes) -> SessionPartialEvent | list[SessionPartialEvent] | None:
        buf = self.streams.get(session_id)
        if buf is None:
            raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
        if len(buf) + len(pcm) > self.max_session_bytes:
            raise LogicError(ErrorCode.invalid_request, "会话音频超过 sidecar 缓存上限")
        buf.extend(pcm)
        if self.partials_per_chunk <= 0:
            return None
        events: list[SessionPartialEvent] = []
        for i in range(self.partials_per_chunk):
            if self.partial_tails is not None and i < len(self.partial_tails):
                tail = self.partial_tails[i]
            else:
                tail = self.text if self.partials_per_chunk == 1 else f"{self.text}-{i+1}"
            events.append(
                SessionPartialEvent(
                    session_id=session_id,
                    stable_text="",
                    tail_text=tail,
                    language=self.language,
                    audio_end_s=len(buf) / 32000,
                )
            )
        return events[0] if len(events) == 1 else events

    def finish_stream(self, session_id: str) -> SessionFinalEvent:
        if session_id not in self.streams:
            raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
        self.streams.pop(session_id)
        self.finished.append(session_id)
        return SessionFinalEvent(session_id=session_id, text=self.text, language=self.language)

    def cancel_stream(self, session_id: str) -> None:
        if session_id not in self.streams:
            raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
        self.streams.pop(session_id)
        self.cancelled.append(session_id)

    def has_stream(self, session_id: str) -> bool:
        return session_id in self.streams

    def drop_all_streams(self) -> None:
        self.streams.clear()
