"""请求体上限与路径规范化。唯一来源：按累计实际字节在 ASGI receive 层限流。"""
from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import Request
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from _contracts import ErrorBody, ErrorCode, LogicError

Receive = Callable[[], Awaitable[dict[str, Any]]]

CHUNK_CONTENT_TYPES = frozenset({"application/octet-stream", "application/x-pcm"})
AUDIO_UPLOAD_NOTE = (
    "只接受单个音频文件；限制按整个请求体累计字节（含 multipart 头），"
    "不是先整段读入内存再拒绝。空体或静音会被拒绝。"
)
IMAGE_UPLOAD_NOTE = "只接受单个图片文件；限制按整个请求体累计字节。"
CHUNK_UPLOAD_NOTE = (
    "识别 chunk 必须是 application/octet-stream（或 application/x-pcm）的 s16le PCM，"
    "长度必须为偶数；上限为 max_chunk_bytes，按累计实际字节在收包时执行。"
)


class BodyLimitExceeded(HTTPException):
    """请求体超过路径上限。继承 HTTPException，避免 FastAPI 把收包异常吞成 400。"""

    def __init__(self, limit: int, received: int) -> None:
        self.limit = limit
        self.received = received
        detail = ErrorBody(
            error=ErrorCode.invalid_request,
            message="请求体超过允许大小",
            details={"limit": limit, "received": received},
        ).model_dump()
        super().__init__(status_code=413, detail=detail)


def canonical_request_path(path: str) -> str | None:
    """解码一次、去掉尾斜杠、拒绝未归一的 `..`。失败则返回 None（调用方应拒绝）。"""
    if not path or not path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    decoded = unquote(path)
    if "\\" in decoded or "\x00" in decoded:
        return None
    parts: list[str] = []
    for segment in decoded.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            return None
        parts.append(segment)
    return "/" + "/".join(parts)


def body_limit_for_path(path: str, *, max_upload_bytes: int, max_chunk_bytes: int) -> int:
    canon = canonical_request_path(path) or path
    if "/recognize/sessions/" in canon and canon.endswith("/chunks"):
        return max_chunk_bytes
    return max_upload_bytes


def wrap_limited_receive(receive: Receive, limit: int) -> Receive:
    """包装 ASGI receive：按块累计，超限立即抛出且不把该块交给下游。不缓存整段请求。"""
    total = 0
    exceeded = False

    async def limited_receive() -> dict[str, Any]:
        nonlocal total, exceeded
        if exceeded:
            raise BodyLimitExceeded(limit, total)
        message = await receive()
        if message.get("type") == "http.request":
            chunk = message.get("body") or b""
            nxt = total + len(chunk)
            if nxt > limit:
                exceeded = True
                total = nxt
                raise BodyLimitExceeded(limit, nxt)
            total = nxt
        return message

    return limited_receive


async def read_body_limited(request: Request, limit: int) -> bytes:
    """受限读取请求体。超限抛 BodyLimitExceeded，缓冲不超过 limit 字节。"""
    buf = bytearray()
    async for chunk in request.stream():
        nxt = len(buf) + len(chunk)
        if nxt > limit:
            raise BodyLimitExceeded(limit, nxt)
        buf.extend(chunk)
    return bytes(buf)


async def read_upload_limited(upload: UploadFile, limit: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        nxt = len(buf) + len(chunk)
        if nxt > limit:
            raise BodyLimitExceeded(limit, nxt)
        buf.extend(chunk)
    return bytes(buf)


def _is_upload(value: Any) -> bool:
    return isinstance(value, UploadFile) or (
        hasattr(value, "read") and getattr(value, "filename", None) is not None
    )


async def read_single_form_file(
    request: Request,
    *,
    field: str,
    limit: int,
    missing: str,
) -> bytes:
    """只接受一个上传文件，按上限分块读取。多文件直接拒绝。"""
    form = await request.form()
    files = [value for _, value in form.multi_items() if _is_upload(value)]
    if len(files) > 1:
        raise LogicError(ErrorCode.invalid_request, "只接受单个上传文件")
    named = form.get(field)
    if named is None or not _is_upload(named):
        raise LogicError(ErrorCode.invalid_request, missing)
    return await read_upload_limited(named, limit)


def ensure_upload_tmp(path: Path) -> Path:
    """multipart 溢出 spool 必须落在给定目录；若进程临时目录是 /tmp 则改到这里。"""
    resolved = path.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    current = Path(tempfile.gettempdir()).resolve()
    text = str(current)
    if current in {Path("/tmp"), Path("/var/tmp")} or "/.pp/tmp/" not in f"{text}/":
        tempfile.tempdir = str(resolved)
    return resolved
