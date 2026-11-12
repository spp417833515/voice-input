"""ASR sidecar 的 HTTP 适配器。应用核心只通过本模块访问识别能力。

禁止在本模块或调用链上 import torch / vLLM。
禁止在失败时自动改走 CPU、Whisper 或其他运行时。
长录音 0.5–1s / 近 120s 路径尚未达标，见 ASR_STREAMING_BLOCKERS，不得报绿。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx

from _contracts import (
    ASR_STREAMING_BLOCKERS,
    SIDECAR_CHUNK_HEADER,
    SIDECAR_DEFAULT_BASE_URL,
    SIDECAR_SESSIONS_PATH,
    SIDECAR_TRANSCRIBE_PATH,
    ChunkAccepted,
    ErrorCode,
    LogicError,
    RecognizeSessionCreate,
    RecognizeSessionCreated,
    SessionClosedEvent,
    SessionClosedReason,
    SessionErrorEvent,
    SessionFinalEvent,
    SessionPartialEvent,
    SessionSSEEvent,
    SttResponse,
)

_FORBIDDEN_IMPORTS = ("torch", "vllm", "faster_whisper", "whisper")


def _assert_no_forbidden_runtime() -> None:
    import sys

    for name in _FORBIDDEN_IMPORTS:
        if name in sys.modules:
            raise LogicError(
                ErrorCode.internal,
                f"应用核心禁止加载识别运行时模块: {name}",
            )


class AsrSidecarClient:
    def __init__(
        self,
        base_url: str = SIDECAR_DEFAULT_BASE_URL,
        timeout_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        _assert_no_forbidden_runtime()
        self.base_url = base_url.rstrip("/")
        self.blockers = ASR_STREAMING_BLOCKERS
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "AsrSidecarClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        _assert_no_forbidden_runtime()
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise LogicError(ErrorCode.timeout, f"ASR sidecar 超时: {exc}") from exc
        except httpx.RequestError as exc:
            raise LogicError(
                ErrorCode.asr_unavailable, f"无法连接 ASR sidecar: {exc}"
            ) from exc
        if resp.status_code >= 400:
            snippet = (resp.text or "")[:300]
            code = ErrorCode.not_found if resp.status_code == 404 else ErrorCode.asr_unavailable
            if resp.status_code in (400, 422):
                code = ErrorCode.invalid_request
            if resp.status_code == 409:
                code = ErrorCode.conflict
            raise LogicError(
                code,
                f"ASR sidecar 返回 HTTP {resp.status_code}",
                details={"status_code": resp.status_code, "body": snippet},
            )
        return resp

    def transcribe(
        self,
        audio: bytes,
        *,
        language: str | None = None,
        context: str | None = None,
        filename: str = "audio.wav",
        content_type: str = "application/octet-stream",
    ) -> SttResponse:
        """离线整段转写。不走 Whisper，失败不降级。"""
        files = {"audio": (filename, audio, content_type)}
        data = {}
        if language is not None:
            data["language"] = language
        if context is not None:
            data["context"] = context
        resp = self._request("POST", SIDECAR_TRANSCRIBE_PATH, files=files, data=data)
        try:
            payload = resp.json()
            return SttResponse.model_validate(payload)
        except Exception as exc:
            raise LogicError(
                ErrorCode.asr_unavailable, f"ASR 转写响应无法解析: {exc}"
            ) from exc

    def create_session(self, body: RecognizeSessionCreate | None = None) -> RecognizeSessionCreated:
        payload = (body or RecognizeSessionCreate()).model_dump(mode="json", exclude_none=True)
        resp = self._request("POST", SIDECAR_SESSIONS_PATH, json=payload)
        try:
            return RecognizeSessionCreated.model_validate(resp.json())
        except Exception as exc:
            raise LogicError(
                ErrorCode.asr_unavailable, f"创建识别会话响应无法解析: {exc}"
            ) from exc

    def send_chunk(self, session_id: str, index: int, pcm: bytes) -> ChunkAccepted:
        if index < 0:
            raise LogicError(ErrorCode.invalid_request, "chunk index 不能为负")
        path = f"{SIDECAR_SESSIONS_PATH}/{session_id}/chunks"
        resp = self._request(
            "POST",
            path,
            content=pcm,
            headers={
                SIDECAR_CHUNK_HEADER: str(index),
                "Content-Type": "application/octet-stream",
            },
        )
        try:
            return ChunkAccepted.model_validate(resp.json())
        except Exception as exc:
            raise LogicError(
                ErrorCode.asr_unavailable, f"分块响应无法解析: {exc}"
            ) from exc

    def finish(self, session_id: str) -> SessionFinalEvent | SessionClosedEvent:
        path = f"{SIDECAR_SESSIONS_PATH}/{session_id}/finish"
        resp = self._request("POST", path)
        try:
            data = resp.json()
            event = data.get("event")
            if event == "final":
                return SessionFinalEvent.model_validate(data)
            return SessionClosedEvent.model_validate(data)
        except LogicError:
            raise
        except Exception as exc:
            raise LogicError(
                ErrorCode.asr_unavailable, f"finish 响应无法解析: {exc}"
            ) from exc

    def cancel(self, session_id: str) -> SessionClosedEvent:
        path = f"{SIDECAR_SESSIONS_PATH}/{session_id}/cancel"
        resp = self._request("POST", path)
        try:
            ev = SessionClosedEvent.model_validate(resp.json())
        except Exception as exc:
            raise LogicError(
                ErrorCode.asr_unavailable, f"cancel 响应无法解析: {exc}"
            ) from exc
        if ev.reason != SessionClosedReason.cancelled:
            raise LogicError(
                ErrorCode.asr_unavailable,
                "cancel 必须返回 closed(cancelled)，不得当作已定稿",
                details={"reason": ev.reason},
            )
        return ev

    def iter_events(
        self,
        session_id: str,
        *,
        attach_response: "Callable[[httpx.Response], None] | None" = None,
    ) -> Iterator[SessionSSEEvent]:
        path = f"{SIDECAR_SESSIONS_PATH}/{session_id}/events"
        try:
            with self._client.stream("GET", path) as resp:
                if attach_response is not None:
                    attach_response(resp)
                if resp.status_code >= 400:
                    snippet = (resp.read().decode("utf-8", errors="replace") if not resp.is_closed else "")[:300]
                    raise LogicError(
                        ErrorCode.asr_unavailable,
                        f"ASR 事件流返回 HTTP {resp.status_code}",
                        details={"status_code": resp.status_code, "body": snippet},
                    )
                yield from _parse_sse_events(resp.iter_lines())
        except LogicError:
            raise
        except httpx.TimeoutException as exc:
            raise LogicError(ErrorCode.timeout, f"ASR 事件流超时: {exc}") from exc
        except httpx.RequestError as exc:
            raise LogicError(ErrorCode.asr_unavailable, f"ASR 事件流中断: {exc}") from exc


def _parse_sse_events(lines: Iterator[str]) -> Iterator[SessionSSEEvent]:
    buf_event: str | None = None
    buf_data: list[str] = []
    for line in lines:
        if line is None:
            continue
        if line.startswith("event:"):
            buf_event = line[6:].strip()
        elif line.startswith("data:"):
            buf_data.append(line[5:].lstrip())
        elif line == "":
            if not buf_data:
                buf_event = None
                continue
            raw = "\n".join(buf_data)
            buf_data = []
            ev_name = buf_event
            buf_event = None
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise LogicError(
                    ErrorCode.asr_unavailable, f"SSE data 不是合法 JSON: {exc}"
                ) from exc
            if ev_name and isinstance(payload, dict) and "event" not in payload:
                payload["event"] = ev_name
            yield _coerce_session_event(payload)
    if buf_data:
        raw = "\n".join(buf_data)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise LogicError(
                ErrorCode.asr_unavailable, f"SSE 尾部不是合法 JSON: {exc}"
            ) from exc
        if buf_event and isinstance(payload, dict) and "event" not in payload:
            payload["event"] = buf_event
        yield _coerce_session_event(payload)


def _coerce_session_event(payload: object) -> SessionSSEEvent:
    if not isinstance(payload, dict):
        raise LogicError(ErrorCode.asr_unavailable, "SSE 事件必须是对象")
    kind = payload.get("event")
    try:
        if kind == "partial":
            return SessionPartialEvent.model_validate(payload)
        if kind == "final":
            return SessionFinalEvent.model_validate(payload)
        if kind == "error":
            return SessionErrorEvent.model_validate(payload)
        if kind == "closed":
            return SessionClosedEvent.model_validate(payload)
    except LogicError:
        raise
    except Exception as exc:
        raise LogicError(ErrorCode.asr_unavailable, f"SSE 事件不符合契约: {exc}") from exc
    raise LogicError(
        ErrorCode.asr_unavailable,
        f"未知的会话 SSE event: {kind!r}",
    )
