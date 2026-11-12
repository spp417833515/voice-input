"""history / config / status / 产品 SSE。"""
from __future__ import annotations

import asyncio

import queue
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from _contracts import (
    ApiStatusResponse,
    AppConfigUpdate,
    AppConfigView,
    ErrorCode,
    HistoryListResponse,
    PlatformEventAccepted,
    PlatformEventCreate,
    ProductErrorEvent,
    ProductIdleEvent,
    ProductSSEEventEnvelope,
)
from _config import apply_config_update, config_view
from _v3.http import format_sse, sse_openapi
from _v3.runtime import Runtime, effective_unmet_gates


def build_control_router(runtime: Runtime) -> APIRouter:
    router = APIRouter(tags=["control"])
    @router.get("/api/v1/history", response_model=HistoryListResponse)
    def history(limit: int = 50, offset: int = 0) -> HistoryListResponse:
        return runtime.history.list_records(limit=limit, offset=offset)

    @router.get("/api/v1/config", response_model=AppConfigView)
    def get_config() -> AppConfigView:
        return config_view(runtime.config.load())

    @router.post("/api/v1/config", response_model=AppConfigView)
    def post_config(body: AppConfigUpdate) -> AppConfigView:
        return apply_config_update(runtime.config, body)

    @router.get("/api/v1/status", response_model=ApiStatusResponse)
    def status() -> ApiStatusResponse:
        return ApiStatusResponse(
            unmet_gates=effective_unmet_gates(runtime),
            injected={
                "transcribe": runtime.transcribe is not None,
                "polish": runtime.polish is not None,
                "translate": runtime.translate is not None,
                "ocr": runtime.ocr is not None,
                "stream_provider": runtime.stream_provider is not None,
            },
        )

    @router.get("/api/v1/events", responses=sse_openapi("ProductSSEEventEnvelope"), response_class=StreamingResponse)
    async def product_events(request: Request) -> StreamingResponse:
        overflow = {"flag": False}
        q: queue.Queue = queue.Queue(maxsize=runtime.sse_queue_max)

        def listener(event):
            try:
                q.put_nowait(event)
            except queue.Full:
                overflow["flag"] = True

        runtime.events.subscribe(listener)

        async def generate():
            try:
                idle = ProductIdleEvent()
                ProductSSEEventEnvelope.model_validate(idle.model_dump())
                yield format_sse(idle)
                while True:
                    if await request.is_disconnected():
                        break
                    if overflow["flag"]:
                        err = ProductErrorEvent(
                            error=ErrorCode.internal,
                            message="SSE 队列已满，连接已断开；请查询任务或历史状态",
                        )
                        ProductSSEEventEnvelope.model_validate(err.model_dump())
                        yield format_sse(err)
                        return
                    try:
                        event = await asyncio.to_thread(q.get, True, 0.2)
                    except queue.Empty:
                        continue
                    ProductSSEEventEnvelope.model_validate(
                        event.model_dump() if hasattr(event, "model_dump") else event
                    )
                    yield format_sse(event)
            finally:
                runtime.events.unsubscribe(listener)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @router.post("/api/v1/events/platform", response_model=PlatformEventAccepted, status_code=202)
    def platform_event(body: PlatformEventCreate) -> PlatformEventAccepted:
        """本机桌面平台壳的状态进入同一产品事件流（状态栏唯一来源）。"""
        runtime.events.publish(body.root)
        return PlatformEventAccepted()

    return router
