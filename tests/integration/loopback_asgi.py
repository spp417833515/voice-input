"""随机回环端口上的最小 ASGI HTTP/1.1 服务器，供并发 SSE + POST。

只用标准库与已有 h11（httpx 依赖），不安装 uvicorn，不占用 17945/17946/11434。
这是测试宿主，不是生产 daemon。
"""
from __future__ import annotations

import asyncio
import socket
import struct
import threading
from collections.abc import Callable
from typing import Any

import h11

FORBIDDEN_PORTS = frozenset({17945, 17946, 11434})


class LoopbackASGIServer:
    """在 127.0.0.1 随机端口上服务一个 ASGI 应用，支持并发连接与流式响应。"""

    def __init__(self, app: Callable, *, host: str = "127.0.0.1") -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("loopback ASGI 只允许回环地址")
        bind_host = "127.0.0.1" if host == "localhost" else host
        self.app = app
        self.host = bind_host
        self._sock = self._bind(bind_host)
        self.port = int(self._sock.getsockname()[1])
        self.base_url = f"http://{self.host}:{self.port}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.AbstractServer | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._lifespan_queue: asyncio.Queue | None = None
        self._lifespan_task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @staticmethod
    def _bind(host: str) -> socket.socket:
        for _ in range(16):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, 0))
            port = int(sock.getsockname()[1])
            if port not in FORBIDDEN_PORTS:
                sock.setblocking(False)
                sock.listen(128)
                return sock
            sock.close()
        raise RuntimeError("无法分配非保留回环端口")

    def start(self) -> str:
        if self._thread is not None:
            return self.base_url
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=f"asgi-loopback-{self.port}", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=8.0):
            self.close()
            raise RuntimeError("loopback ASGI 未在时限内就绪")
        if self._error is not None:
            err = self._error
            self.close()
            raise RuntimeError(f"loopback ASGI 启动失败: {err}") from err
        return self.base_url

    def close(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is not None and loop.is_running() and self._stop is not None:
            loop.call_soon_threadsafe(self._stop.set)
        if thread is not None:
            thread.join(timeout=8.0)
            self._thread = None
        self._loop = None
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "LoopbackASGIServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _run(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(self._drain())
            except Exception:
                pass
            loop.close()

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        await self._lifespan_startup()
        self._server = await asyncio.start_server(self._handle, sock=self._sock)
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            await self._close_connections()
            server = self._server
            if server is not None:
                server.close()
                try:
                    await asyncio.wait_for(server.wait_closed(), timeout=2.0)
                except Exception:
                    pass
                self._server = None
            await self._lifespan_shutdown()

    async def _drain(self) -> None:
        pending = [task for task in asyncio.all_tasks(self._loop) if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


    async def _close_connections(self) -> None:
        writers = list(self._writers)
        for writer in writers:
            try:
                sock = writer.get_extra_info("socket")
                if sock is not None:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                writer.close()
            except Exception:
                pass
        self._writers.clear()

    async def _lifespan_startup(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        self._lifespan_queue = queue
        started = asyncio.Event()
        failed: list[BaseException] = []

        async def receive() -> dict[str, Any]:
            return await queue.get()

        async def send(message: dict[str, Any]) -> None:
            typ = message.get("type")
            if typ == "lifespan.startup.complete":
                started.set()
            elif typ == "lifespan.startup.failed":
                failed.append(RuntimeError(message.get("message") or "lifespan startup failed"))
                started.set()
            elif typ == "lifespan.shutdown.complete":
                pass
            elif typ == "lifespan.shutdown.failed":
                failed.append(RuntimeError(message.get("message") or "lifespan shutdown failed"))

        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.3"}, "state": {}}
        self._lifespan_task = asyncio.create_task(self.app(scope, receive, send))
        await queue.put({"type": "lifespan.startup"})
        try:
            await asyncio.wait_for(started.wait(), timeout=5.0)
        except TimeoutError as exc:
            raise RuntimeError("ASGI lifespan 启动超时") from exc
        if failed:
            raise failed[0]

    async def _lifespan_shutdown(self) -> None:
        queue = self._lifespan_queue
        task = self._lifespan_task
        if queue is None or task is None:
            return
        await queue.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        except Exception:
            task.cancel()
        self._lifespan_task = None
        self._lifespan_queue = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h11.Connection(h11.SERVER)
        peer = writer.get_extra_info("peername") or (self.host, 0)
        self._writers.add(writer)
        try:
            while True:
                request = await self._read_request(conn, reader)
                if request is None:
                    break
                await self._run_http(conn, reader, writer, request, peer)
                if conn.our_state is h11.MUST_CLOSE or conn.their_state is h11.MUST_CLOSE:
                    break
                if conn.our_state is h11.DONE and conn.their_state is h11.DONE:
                    conn.start_next_cycle()
                    continue
                break
        except (ConnectionError, h11.ProtocolError, asyncio.IncompleteReadError, TimeoutError, RuntimeError):
            pass
        except Exception:
            pass
        finally:
            self._writers.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _next_event(self, conn: h11.Connection, reader: asyncio.StreamReader) -> object:
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                data = await reader.read(65536)
                conn.receive_data(data if data else b"")
                if not data:
                    event = conn.next_event()
                    return event
                continue
            return event

    async def _read_request(
        self, conn: h11.Connection, reader: asyncio.StreamReader
    ) -> tuple[h11.Request, bytes] | None:
        request: h11.Request | None = None
        body = bytearray()
        while True:
            event = await self._next_event(conn, reader)
            if event is h11.NEED_DATA:
                return None
            if isinstance(event, h11.ConnectionClosed):
                return None
            if isinstance(event, h11.Request):
                request = event
            elif isinstance(event, h11.Data):
                body.extend(event.data)
            elif isinstance(event, h11.EndOfMessage):
                if request is None:
                    return None
                return request, bytes(body)
            elif event is h11.PAUSED:
                continue

    async def _run_http(
        self,
        conn: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: tuple[h11.Request, bytes],
        peer: tuple[str, int] | tuple,
    ) -> None:
        req, body = request
        target = req.target.decode("latin1")
        if "?" in target:
            path, query = target.split("?", 1)
        else:
            path, query = target, ""
        method = req.method.decode("ascii")
        headers = [(name.lower(), value) for name, value in req.headers]
        disconnect: asyncio.Queue = asyncio.Queue()
        body_sent = False
        completed = asyncio.Event()
        watch = asyncio.create_task(self._watch_disconnect(reader, disconnect, completed))

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await disconnect.get()

        async def send(message: dict[str, Any]) -> None:
            typ = message["type"]
            if typ == "http.response.start":
                raw_headers = list(message.get("headers") or [])
                data = conn.send(h11.Response(status_code=int(message["status"]), headers=raw_headers))
                if data:
                    writer.write(data)
                    await writer.drain()
            elif typ == "http.response.body":
                chunk = message.get("body") or b""
                if chunk:
                    data = conn.send(h11.Data(data=chunk))
                    if data:
                        writer.write(data)
                        await writer.drain()
                if not message.get("more_body", False):
                    data = conn.send(h11.EndOfMessage())
                    if data:
                        writer.write(data)
                        await writer.drain()
                    completed.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii", "replace"),
            "query_string": query.encode("latin1"),
            "headers": headers,
            "client": (peer[0], int(peer[1])) if peer and len(peer) >= 2 else (self.host, 0),
            "server": (self.host, self.port),
            "state": {},
        }
        try:
            await self.app(scope, receive, send)
        except Exception:
            if conn.our_state is h11.IDLE or conn.our_state is h11.SEND_RESPONSE:
                try:
                    data = conn.send(h11.Response(status_code=500, headers=[(b"content-length", b"0")]))
                    if data:
                        writer.write(data)
                    end = conn.send(h11.EndOfMessage())
                    if end:
                        writer.write(end)
                    await writer.drain()
                except Exception:
                    pass
            raise
        finally:
            completed.set()
            watch.cancel()
            try:
                await watch
            except asyncio.CancelledError:
                pass

    async def _watch_disconnect(
        self, reader: asyncio.StreamReader, disconnect: asyncio.Queue, completed: asyncio.Event
    ) -> None:
        try:
            while not completed.is_set():
                data = await reader.read(65536)
                if not data:
                    await disconnect.put({"type": "http.disconnect"})
                    return
        except Exception:
            try:
                await disconnect.put({"type": "http.disconnect"})
            except Exception:
                pass
