"""测试用能力提供者。明确不是识别引擎，不拼接字幕，不宣称官方 sidecar 可用。"""
from __future__ import annotations

from _contracts import (
    RecognizeSessionCreate,
    SessionFinalEvent,
    SttResponse,
)


class FixtureAsrProvider:
    """会话流式夹具：finish 返回预设文本，不把 PCM 当字符串拼接。"""

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

    def push_pcm(self, session_id: str, index: int, pcm: bytes) -> None:
        del pcm  # 夹具不解析、不拼接音频内容
        self.chunks.setdefault(session_id, []).append(index)

    def finish(self, session_id: str) -> SessionFinalEvent:
        return SessionFinalEvent(session_id=session_id, text=self.final_text, language=self.language)

    def cancel(self, session_id: str) -> None:
        self.cancelled.add(session_id)


def fixture_transcribe(audio: bytes, *, language: str | None = None, context: str | None = None) -> SttResponse:
    del context
    try:
        text = audio.decode("utf-8")
        if text and text.isprintable() and "\x00" not in text:
            return SttResponse(text=text, language=language or "zh")
    except UnicodeDecodeError:
        pass
    return SttResponse(text="fixture-transcript", language=language or "zh")


def fixture_ocr(image_bytes: bytes, **_kwargs) -> str:
    if image_bytes == b"empty-image":
        return "  "
    try:
        return image_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "fixture-ocr-text"
