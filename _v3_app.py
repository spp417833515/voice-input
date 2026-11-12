"""v3 HTTP 应用工厂。必须传入显式 Runtime，无参不会指向 live 存储。

本模块只构造 ASGI 应用，不启动服务、不加载模型。生产组合见 _v3/production.py。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from _contracts import ErrorCode
from _v3.auth import (
    access_mode,
    extract_token,
    forbidden,
    is_lan_client,
    is_local_client,
    origin_allowed,
    request_host_allowed,
    token_matches,
)
from _v3.capability_routes import build_capability_router
from _v3.control_routes import build_control_router
from _v3.http import error_body, install_exception_handlers, install_openapi
from _v3.jobs_routes import build_jobs_router
from _v3.limits import (
    BodyLimitExceeded,
    body_limit_for_path,
    canonical_request_path,
    ensure_upload_tmp,
    wrap_limited_receive,
)
from _v3.recognize_routes import build_recognize_router
from _v3.pages import install_pages
from _v3.production import compose_production_runtime
from _v3.runtime import Runtime, UnmetRuntimeGates, build_sandbox_runtime, production_unmet_gates
from _v3.setup_routes import build_setup_router
from _v3.sessions import RecognizeSessionHub
from _v3.utterance_routes import build_utterance_router

_DESCRIPTION = """
v3 `/api/v1` 与 `/api/setup/*` 路由，以及 static/v3 唯一界面入口。是否已可上线只看 unmet_gates。

鉴权：本机以 socket 对端 loopback（127.0.0.1 / ::1，含 IPv4-mapped）判定，忽略 X-Forwarded-For。
局域网路径：先判定对端属于 RFC1918、IPv6 ULA 或必要链路本地，再恒定时间校验 token。
公网、198.18/15、TEST-NET、保留网段以及无法解析的对端，即使 token 正确也 403。
仅本机路径即使带 token 也不对远程放行。
Host 必须带服务端口：本机请求只能是 127.0.0.1 / localhost / ::1；局域网请求必须是允许的局域网地址。
缺 Origin 视为非浏览器，不做 CSRF Origin 检查，但仍受对端 / token / Host 约束（防 DNS rebinding）。
若存在 Origin，必须是 https 且与 Host、对端类型一致；本机 Origin 不能搭私网 Host；未知 Origin 拒绝。不开 CORS *。
GET /api/v1/status 的 ok 只表示本接口响应成功，不是可上线；是否达切流条件只看 unmet_gates。
SSE 走 HTTP，不使用 WebSocket。请求体在 ASGI receive 层按累计实际字节限流，超限 413，不把超额字节喂给下游。
""".strip()


class AccessGuardMiddleware:
    """纯 ASGI 中间件，避免 BaseHTTPMiddleware 缓冲 SSE 响应体。"""

    def __init__(self, app, runtime: Runtime) -> None:
        self.app = app
        self._runtime = runtime

    def _limit_for(self, path: str) -> int:
        return body_limit_for_path(
            path,
            max_upload_bytes=self._runtime.max_upload_bytes,
            max_chunk_bytes=self._runtime.max_chunk_bytes,
        )

    def _authorize(self, request: Request, path: str) -> JSONResponse | None:
        local = is_local_client(request)
        if not request_host_allowed(request, is_local=local, port=self._runtime.auth.port):
            return JSONResponse(status_code=403, content=forbidden("缺少或不可信的 Host"))
        mode = access_mode(request.method, path)
        if mode == "local" and not local:
            return JSONResponse(status_code=403, content=forbidden("该接口仅本机可访问"))
        if mode == "lan_public" and not local and not is_lan_client(request):
            return JSONResponse(status_code=403, content=forbidden("该页面仅本机或允许的局域网可访问"))
        if mode == "lan_token" and not local:
            if not is_lan_client(request):
                return JSONResponse(
                    status_code=403,
                    content=forbidden("该接口仅本机或允许的局域网可访问"),
                )
            if not token_matches(extract_token(request), self._runtime.auth.token):
                return JSONResponse(status_code=403, content=forbidden("token 无效或缺失"))
        origin = request.headers.get("origin")
        if origin:
            if not origin_allowed(
                origin,
                is_local=local,
                request_host=request.headers.get("host"),
                port=self._runtime.auth.port,
            ):
                return JSONResponse(
                    status_code=403,
                    content=forbidden("跨站请求被拒绝 (CSRF 防护)"),
                )
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_path = scope.get("path") or "/"
        path = canonical_request_path(raw_path)
        if path is None:
            blocked = JSONResponse(
                status_code=400,
                content=error_body(ErrorCode.invalid_request, "请求路径无效"),
            )
            await blocked(scope, receive, send)
            return
        if path != raw_path:
            scope = dict(scope)
            scope["path"] = path
            scope["raw_path"] = path.encode("ascii", "ignore") or b"/"
        request = Request(scope, receive)
        limit = self._limit_for(path)
        cl = request.headers.get("content-length")
        if cl is not None and cl != "":
            try:
                length = int(cl)
                if length < 0:
                    raise ValueError
            except ValueError:
                blocked = JSONResponse(
                    status_code=400,
                    content=error_body(ErrorCode.invalid_request, "Content-Length 无效"),
                )
                await blocked(scope, receive, send)
                return
            if length > limit:
                blocked = JSONResponse(
                    status_code=413,
                    content=error_body(
                        ErrorCode.invalid_request,
                        "请求体超过允许大小",
                        details={"limit": limit, "content_length": length},
                    ),
                )
                await blocked(scope, receive, send)
                return
        blocked = self._authorize(request, path)
        if blocked is not None:
            await blocked(scope, receive, send)
            return
        limited_receive = wrap_limited_receive(receive, limit)
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except BodyLimitExceeded as exc:
            if started:
                raise
            response = JSONResponse(
                status_code=413,
                content=error_body(
                    ErrorCode.invalid_request,
                    "请求体超过允许大小",
                    details={"limit": exc.limit, "received": exc.received},
                ),
            )
            await response(scope, receive, send)


def create_app(runtime: Runtime, *, static_v3: Path | None = None) -> FastAPI:
    """static_v3 给出时挂唯一生产界面（static/v3）；不给则只有 API。"""
    if not isinstance(runtime, Runtime):
        raise TypeError("create_app 必须传入显式 Runtime，禁止无参默认指向 live 存储")
    if runtime.upload_tmp is not None:
        ensure_upload_tmp(runtime.upload_tmp)
    app = FastAPI(
        title="ollama-voice-input v1",
        version="0.0.0-v3-api",
        description=_DESCRIPTION,
        docs_url=None,
        redoc_url=None,
    )
    app.state.runtime = runtime
    install_exception_handlers(app)
    app.add_middleware(AccessGuardMiddleware, runtime=runtime)
    hub = RecognizeSessionHub(
        finals=runtime.finals,
        provider=runtime.stream_provider,
        max_chunk_bytes=runtime.max_chunk_bytes,
        max_session_pcm_bytes=runtime.max_session_pcm_bytes,
        clock=runtime.clock,
        retain_closed_s=runtime.session_retain_closed_s,
        idle_open_s=runtime.session_idle_open_s,
        sse_queue_max=runtime.sse_queue_max,
        product_events=runtime.events,
    )
    app.state.sessions = hub
    app.include_router(build_recognize_router(hub, runtime.max_chunk_bytes))
    app.include_router(build_utterance_router(runtime))
    app.include_router(build_jobs_router(runtime))
    app.include_router(build_capability_router(runtime))
    app.include_router(build_control_router(runtime))
    app.include_router(build_setup_router(runtime))
    if static_v3 is not None:
        install_pages(app, static_v3)
    install_openapi(
        app,
        title="ollama-voice-input v1",
        version="0.0.0-v3-api",
        description=_DESCRIPTION,
    )
    return app


def export_openapi_document(*, root: Path, token: str = "test-openapi-export-token") -> dict:
    """从真实 FastAPI 应用派生 OpenAPI，不手写 paths。"""
    runtime = build_sandbox_runtime(root=root, token=token, unmet_gates=("openapi export sandbox",))
    app = create_app(runtime)
    return app.openapi()


__all__ = [
    "AccessGuardMiddleware",
    "Runtime",
    "UnmetRuntimeGates",
    "build_sandbox_runtime",
    "compose_production_runtime",
    "create_app",
    "export_openapi_document",
    "production_unmet_gates",
]



