"""HTTP 错误映射、上传上限、SSE 编码与 OpenAPI 合并。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from _contracts import ErrorBody, ErrorCode, LogicError, build_openapi_skeleton, contract_models
from _v3.auth import access_mode
from _v3.limits import BodyLimitExceeded

ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.invalid_request: 400,
    ErrorCode.not_found: 404,
    ErrorCode.conflict: 409,
    ErrorCode.unauthorized: 401,
    ErrorCode.forbidden: 403,
    ErrorCode.timeout: 504,
    ErrorCode.dependency_failed: 503,
    ErrorCode.asr_unavailable: 503,
    ErrorCode.polish_failed: 503,
    ErrorCode.translate_failed: 503,
    ErrorCode.ocr_failed: 503,
    ErrorCode.ocr_language_missing: 503,
    ErrorCode.history_io: 500,
    ErrorCode.jobs_io: 500,
    ErrorCode.config_io: 500,
    ErrorCode.internal: 500,
}


def error_response(exc: LogicError) -> JSONResponse:
    status = ERROR_STATUS.get(exc.code, 500)
    return JSONResponse(status_code=status, content=jsonable_encoder(exc.to_body()))


def error_body(code: ErrorCode, message: str, *, details: dict[str, Any] | None = None) -> dict:
    return ErrorBody(error=code, message=message, details=details).model_dump()


def logic_http_exception(exc: LogicError) -> JSONResponse:
    return error_response(exc)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(LogicError)
    async def _logic(_request: Request, exc: LogicError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(BodyLimitExceeded)
    async def _too_large(_request: Request, exc: BodyLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content=error_body(
                ErrorCode.invalid_request,
                "请求体超过允许大小",
                details={"limit": exc.limit, "received": exc.received},
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validate(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_body(
                ErrorCode.invalid_request,
                "请求无法通过校验",
                details={"errors": exc.errors()},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail and "message" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        code = ErrorCode.forbidden if exc.status_code in {401, 403} else ErrorCode.invalid_request
        if exc.status_code == 404:
            code = ErrorCode.not_found
        if exc.status_code == 409:
            code = ErrorCode.conflict
        if exc.status_code == 413:
            code = ErrorCode.invalid_request
        if exc.status_code == 503:
            code = ErrorCode.dependency_failed
        message = detail if isinstance(detail, str) else str(detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, message),
        )


def format_sse(event: Any) -> str:
    payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
    name = payload.get("event") if isinstance(payload, dict) else None
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if name:
        return f"event: {name}\ndata: {data}\n\n"
    return f"data: {data}\n\n"


def sse_openapi(schema_name: str) -> dict[str, Any]:
    return {
        200: {
            "description": "text/event-stream；每条 data 为对应契约模型 JSON。",
            "content": {
                "text/event-stream": {
                    "schema": {"$ref": f"#/components/schemas/{schema_name}"},
                }
            },
        }
    }



_ERROR_BODY_REF = {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}}


def apply_runtime_security(schema: dict[str, Any]) -> None:
    """各 operation 的 403/413 与 security 由同一 access_mode 派生，不手写第二份权限表。"""
    for path, ops in (schema.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.startswith("x-") or not isinstance(op, dict):
                continue
            mode = access_mode(method.upper(), path)
            responses = op.setdefault("responses", {})
            responses.setdefault(
                "403",
                {"description": "鉴权失败：非本机、对端不是允许的局域网、token 无效、Host/Origin 不可信。", "content": _ERROR_BODY_REF},
            )
            if method.upper() in {"POST", "PUT", "PATCH"}:
                responses.setdefault(
                    "413",
                    {"description": "请求体超过该路径的累计字节上限（缺 Content-Length 时同样生效）。", "content": _ERROR_BODY_REF},
                )
            if mode == "lan_token":
                op["security"] = [{"VoiceToken": []}]
            else:
                op.setdefault("security", [])
            if path == "/api/v1/status" and method == "get":
                desc = op.get("description") or op.get("summary") or ""
                note = "ok 只表示本接口响应成功，不是可上线或已切流；是否达切流条件只看 unmet_gates。"
                if note not in desc:
                    op["description"] = (desc + " " + note).strip()


def install_openapi(app: FastAPI, *, title: str, version: str, description: str) -> None:
    def _openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=title,
            version=version,
            description=description,
            routes=app.routes,
        )
        skeleton = build_openapi_skeleton()
        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        for name, item in skeleton["components"]["schemas"].items():
            schemas.setdefault(name, item)
        for model in contract_models():
            if model.__name__ not in schemas:
                schemas[model.__name__] = model.model_json_schema()
        schemes = components.setdefault("securitySchemes", {})
        schemes["VoiceToken"] = {
            "type": "apiKey",
            "in": "header",
            "name": "X-Voice-Token",
            "description": (
                "局域网路径需要 token。也接受 Authorization: Bearer。"
                "仅本机路径即使带 token 也不对远程放行。"
                "不要把真实生产 token 写入示例。"
            ),
        }
        schema.setdefault("x-auth-matrix", {
            "local_socket": "只看 socket 对端 loopback（127.0.0.1 / ::1，含 IPv4-mapped）；忽略 X-Forwarded-For",
            "lan_peer": "仅 RFC1918、IPv6 ULA fc00::/7、链路本地 169.254/16 与 fe80::/10。198.18/15、TEST-NET、其他保留/文档网段与公网都不是局域网。无法解析的对端拒绝。IPv4-mapped IPv6 先还原再判定。",
            "lan_token": "本机 loopback 免 token；或先判定对端为允许的局域网再恒定时间校验 token。不是任意对端+token。公网即使 token 正确也 403。",
            "host": "必须显式带服务端口。本机请求 Host 只能是 127.0.0.1 / localhost / ::1；局域网请求 Host 必须是允许的局域网地址。缺 Origin 的非浏览器请求仍受 Host 约束，防 DNS rebinding。TestClient 默认 Host testserver 不在白名单，测试须显式配置可信 Host，生产不得放行任意 Host。",
            "origin": "若存在 Origin，必须 https、端口正确，且与 Host 和对端类型一致：本机 Origin 不能搭私网 Host。未知 Origin 拒绝。缺 Origin 视为非浏览器，跳过 CSRF Origin 检查，但不等于未知 Origin，仍受对端 / token / Host 约束。无 CORS *。",
            "status_ok": "GET /api/v1/status 的 ok 只表示本接口响应成功，不是可上线；未满足的切流条件在 unmet_gates。",
        })
        apply_runtime_security(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = _openapi  # type: ignore[method-assign]
