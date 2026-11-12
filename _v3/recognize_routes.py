"""本机识别会话 HTTP + SSE。"""
from __future__ import annotations

import asyncio
import queue

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from _contracts import (
    SessionErrorEvent,
    SIDECAR_CHUNK_HEADER,
    ChunkAccepted,
    ErrorCode,
    LogicError,
    RecognizeSessionCreate,
    RecognizeSessionCreated,
    SessionClosedEvent,
    SessionSSEEventEnvelope,
)
from _v3.http import format_sse, sse_openapi
from _v3.limits import CHUNK_CONTENT_TYPES, CHUNK_UPLOAD_NOTE, read_body_limited
from _v3.sessions import RecognizeSessionHub


def build_recognize_router(hub: RecognizeSessionHub, max_chunk_bytes: int) -> APIRouter:
    router = APIRouter(tags=["recognize"])
    @router.post("/api/v1/recognize/sessions", response_model=RecognizeSessionCreated)
    def create_session(body: RecognizeSessionCreate) -> RecognizeSessionCreated:
        return hub.create(body)

    @router.post(
        "/api/v1/recognize/sessions/{session_id}/chunks",
        response_model=ChunkAccepted,
        status_code=202,
        description=CHUNK_UPLOAD_NOTE,
    )
    async def upload_chunk(
        session_id: str,
        request: Request,
        x_chunk_index: str | None = Header(default=None, alias=SIDECAR_CHUNK_HEADER),
    ) -> ChunkAccepted:
        if x_chunk_index is None or not str(x_chunk_index).strip():
            raise LogicError(ErrorCode.invalid_request, f"缺少 {SIDECAR_CHUNK_HEADER}")
        try:
            index = int(x_chunk_index)
        except ValueError as exc:
            raise LogicError(ErrorCode.invalid_request, "chunk index 必须是整数") from exc
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and ctype not in CHUNK_CONTENT_TYPES:
            raise LogicError(ErrorCode.invalid_request, "chunk 必须是 application/octet-stream")
        pcm = await read_body_limited(request, max_chunk_bytes)
        return hub.accept_chunk(session_id, index, pcm)

    @router.get(
        "/api/v1/recognize/sessions/{session_id}/events",
        responses=sse_openapi("SessionSSEEventEnvelope"),
        response_class=StreamingResponse,
    )
    async def session_events(session_id: str, request: Request) -> StreamingResponse:
        subscriber = hub.subscribe(session_id)

        async def generate():
            try:
                while True:
                    if await request.is_disconnected():
                        hub.mark_disconnected(session_id)
                        break
                    if subscriber.overflow:
                        overflow = SessionErrorEvent(
                            session_id=session_id,
                            error=ErrorCode.internal,
                            message="SSE 队列已满，连接已断开；请查询会话状态",
                        )
                        SessionSSEEventEnvelope.model_validate(overflow.model_dump())
                        yield format_sse(overflow)
                        hub.mark_disconnected(session_id)
                        return
                    try:
                        event = await asyncio.to_thread(subscriber.get, True, 0.2)
                    except queue.Empty:
                        continue
                    SessionSSEEventEnvelope.model_validate(
                        event.model_dump() if hasattr(event, "model_dump") else event
                    )
                    yield format_sse(event)
                    if getattr(event, "event", None) == "closed":
                        return
            except asyncio.CancelledError:
                hub.mark_disconnected(session_id)
                raise
            finally:
                hub.unsubscribe(session_id, subscriber)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @router.post(
        "/api/v1/recognize/sessions/{session_id}/finish",
        response_model=SessionClosedEvent,
    )
    def finish(session_id: str) -> SessionClosedEvent:
        return hub.finish(session_id)

    @router.post(
        "/api/v1/recognize/sessions/{session_id}/cancel",
        response_model=SessionClosedEvent,
    )
    def cancel(session_id: str) -> SessionClosedEvent:
        return hub.cancel(session_id)

    return router
