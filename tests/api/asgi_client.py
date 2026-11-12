"""原始 ASGI 回环，不启生产端口、不经过会改写 Content-Length 的 TestClient。"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable


async def call_asgi(
    app,
    *,
    method: str,
    path: str,
    client: tuple[str, int],
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    add_content_length: bool = True,
    chunked: bool = False,
    content_length: str | None = None,
    server: tuple[str, int] = ("127.0.0.1", 17945),
) -> dict[str, Any]:
    header_list: list[tuple[bytes, bytes]] = []
    for key, value in (headers or {}).items():
        if key.lower() == "content-length":
            continue
        header_list.append((key.lower().encode(), value.encode() if isinstance(value, str) else value))
    if content_length is not None:
        header_list.append((b"content-length", str(content_length).encode()))
    elif add_content_length and method in {"POST", "PUT", "PATCH"}:
        header_list.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": header_list,
        "client": client,
        "server": server,
    }
    if chunked and body:
        mid = max(1, len(body) // 2)
        parts = [body[:mid], body[mid:]]
    else:
        parts = [body]
    state = {"i": 0}
    messages: list[dict[str, Any]] = []

    async def receive():
        i = state["i"]
        if i < len(parts):
            state["i"] += 1
            return {"type": "http.request", "body": parts[i], "more_body": i < len(parts) - 1}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    header_map = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    try:
        payload = json.loads(raw.decode() or "null")
    except json.JSONDecodeError:
        payload = raw.decode("utf-8", "replace")
    return {"status": start["status"], "headers": header_map, "body": payload, "raw": raw}


def multipart(fields: list[tuple[str, bytes, str | None]], boundary: str = "----bnd") -> tuple[bytes, str]:
    chunks: list[bytes] = []
    for name, data, filename in fields:
        chunks.append(f"--{boundary}\r\n".encode())
        disp = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disp += f'; filename="{filename}"'
        extra = "\r\nContent-Type: application/octet-stream" if filename is not None else ""
        chunks.append((disp + extra + "\r\n\r\n").encode())
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def parse_sse(raw: bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8", "replace")
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        payload = json.loads("\n".join(data_lines))
        if name and isinstance(payload, dict) and "event" not in payload:
            payload["event"] = name
        events.append(payload)
    return events


async def collect_sse(
    app,
    *,
    path: str,
    client: tuple[str, int],
    headers: dict[str, str] | None = None,
    timeout: float = 2.0,
    after_idle: Callable[[], None] | None = None,
    after_start: Callable[[], None] | None = None,
    disconnect_after_events: int | None = None,
    server: tuple[str, int] = ("127.0.0.1", 17945),
) -> dict[str, Any]:
    """有时限的原始 ASGI SSE 采集。可在首个 idle/事件后注入回调或主动断开。"""
    header_list = []
    for key, value in (headers or {}).items():
        header_list.append((key.lower().encode(), value.encode() if isinstance(value, str) else value))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": header_list,
        "client": client,
        "server": server,
    }
    body = bytearray()
    start: dict[str, Any] = {}
    disconnect = asyncio.Event()
    received_request = {"done": False}
    finished = asyncio.Event()
    after_called = {"done": False}

    async def receive():
        if not received_request["done"]:
            received_request["done"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    def maybe_callback() -> None:
        if after_idle is None or after_called["done"]:
            return
        events = parse_sse(bytes(body))
        if events:
            after_called["done"] = True
            after_idle()

    def maybe_disconnect() -> None:
        events = parse_sse(bytes(body))
        if disconnect_after_events is not None and len(events) >= disconnect_after_events:
            disconnect.set()

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
            if after_start is not None:
                after_start()
            if disconnect_after_events == 0:
                disconnect.set()
        elif message["type"] == "http.response.body":
            body.extend(message.get("body") or b"")
            maybe_callback()
            maybe_disconnect()
            if not message.get("more_body", False):
                disconnect.set()
                finished.set()

    async def runner():
        try:
            await app(scope, receive, send)
        finally:
            finished.set()

    task = asyncio.create_task(runner())
    try:
        await asyncio.wait_for(finished.wait(), timeout=timeout)
    except TimeoutError:
        disconnect.set()
        await asyncio.wait_for(finished.wait(), timeout=1.0)
    finally:
        disconnect.set()
        await asyncio.wait_for(task, timeout=1.0)
    header_map = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    return {
        "status": start.get("status"),
        "headers": header_map,
        "raw": bytes(body),
        "events": parse_sse(bytes(body)),
    }
