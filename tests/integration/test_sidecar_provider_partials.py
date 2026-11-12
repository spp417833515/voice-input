"""HTTP FakeEngine → sidecar SSE → 真实 SidecarStreamProvider → 应用 session SSE。

本文件不得使用 FixtureStreamProvider 证明 mid-stream 字幕。
不启 GPU、不用 17945/17946/11434。
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from _asr import AsrSidecarClient
from _asr_provider import SidecarStreamProvider
from _contracts import SIDECAR_CHUNK_HEADER, SessionClosedReason
from _v3.isolated import FORBIDDEN_PORTS, compose_isolated_candidate
from runtimes.asr.server import create_sidecar_app
from tests.api.asgi_client import call_asgi, parse_sse
from tests.integration.fixtures import fixture_transcribe
from tests.integration.loopback_asgi import LoopbackASGIServer
from tests.sidecar.fake_engine import FakeEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PCM = b"\x01\x00" * 16
FORBIDDEN = tuple(sorted(FORBIDDEN_PORTS))


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def _leftover_reader_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("sidecar-sse-")]


def _app_headers(comp) -> dict[str, str]:
    return {
        "host": f"{comp.bind_host}:{comp.bind_port}",
        "origin": f"https://{comp.bind_host}:{comp.bind_port}",
    }


@contextmanager
def fake_sidecar(*, engine: FakeEngine | None = None) -> Iterator[tuple[FakeEngine, LoopbackASGIServer, AsrSidecarClient, SidecarStreamProvider]]:
    engine = engine or FakeEngine(text="sidecar-fixture-final")
    server = LoopbackASGIServer(create_sidecar_app(engine))
    server.start()
    assert server.port not in FORBIDDEN_PORTS
    assert not _port_open(17945)
    assert not _port_open(17946)
    client = AsrSidecarClient(base_url=server.base_url)
    provider = SidecarStreamProvider(client)
    try:
        yield engine, server, client, provider
    finally:
        provider.close()
        client.close()
        port = server.port
        server.close()
        still = [t.name for t in threading.enumerate() if t.name.startswith("sidecar-sse-") or t.name == f"asgi-loopback-{port}"]
        assert not still, f"关闭后仍有残留线程: {still}"
        assert not _port_open(port), f"sidecar 端口未释放: {port}"
        for reserved in FORBIDDEN:
            assert not _port_open(reserved), f"保留端口被占用: {reserved}"


class _AppSsePump:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.started = threading.Event()
        self.first_partial = threading.Event()
        self.closed = threading.Event()
        self.error: BaseException | None = None
        self.status: int | None = None
        self.request_disconnect = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def kinds(self) -> list[str]:
        with self._lock:
            return [item.get("event") for item in self.events]

    def tails(self) -> list[str]:
        with self._lock:
            return [item.get("tail_text") or "" for item in self.events if item.get("event") == "partial"]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.events)

    def wait_partials(self, count: int, *, timeout: float = 5.0) -> list[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tails = self.tails()
            if len(tails) >= count:
                return tails
            time.sleep(0.02)
        raise AssertionError(f"在 {timeout}s 内未收到 {count} 个 partial，当前={self.snapshot()}")

    def wait_closed(self, *, timeout: float = 5.0) -> None:
        if not self.closed.wait(timeout=timeout):
            raise AssertionError(f"应用 SSE 未在时限内 closed，当前={self.snapshot()} error={self.error}")
        if self.error is not None:
            raise AssertionError(f"应用 SSE 泵异常: {self.error}")


def start_app_sse(app, *, path: str, headers: dict[str, str], bind: tuple[str, int]) -> _AppSsePump:
    pump = _AppSsePump()

    async def _run() -> None:
        header_list = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
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
            "client": ("127.0.0.1", 50000),
            "server": bind,
        }
        body = bytearray()
        disconnect = asyncio.Event()

        async def watch_request():
            while not disconnect.is_set():
                if pump.request_disconnect.is_set():
                    disconnect.set()
                    return
                await asyncio.sleep(0.02)

        async def receive():
            if not hasattr(receive, "sent"):
                receive.sent = True  # type: ignore[attr-defined]
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        def ingest(raw: bytes) -> None:
            events = parse_sse(raw)
            with pump._lock:
                pump.events = events
                if any(item.get("event") == "partial" and (item.get("tail_text") or "") for item in events):
                    pump.first_partial.set()
                if any(item.get("event") == "closed" for item in events):
                    pump.closed.set()
                    disconnect.set()

        async def send(message):
            typ = message["type"]
            if typ == "http.response.start":
                pump.status = message.get("status")
                pump.started.set()
            elif typ == "http.response.body":
                body.extend(message.get("body") or b"")
                ingest(bytes(body))
                if not message.get("more_body", False):
                    disconnect.set()
                    pump.closed.set()

        watcher = asyncio.create_task(watch_request())
        try:
            await app(scope, receive, send)
        finally:
            disconnect.set()
            watcher.cancel()
            pump.started.set()
            if any(item.get("event") == "closed" for item in pump.snapshot()):
                pump.closed.set()

    def _thread() -> None:
        try:
            asyncio.run(_run())
        except Exception as exc:
            pump.error = exc
            pump.started.set()
            pump.closed.set()

    pump._thread = threading.Thread(target=_thread, name="app-sse-pump", daemon=True)
    pump._thread.start()
    if not pump.started.wait(timeout=5.0):
        raise AssertionError("应用 session SSE 未在时限内启动")
    if pump.error is not None:
        raise AssertionError(f"应用 session SSE 启动失败: {pump.error}")
    assert pump.status == 200, f"session SSE HTTP {pump.status}"
    return pump


def stop_app_sse(pump: _AppSsePump) -> None:
    pump.request_disconnect.set()
    if pump._thread is not None:
        pump._thread.join(timeout=5.0)
        assert not pump._thread.is_alive(), "应用 SSE 泵线程残留"


@contextmanager
def isolated_sidecar_app(tmp_path: Path, provider: SidecarStreamProvider):
    comp = compose_isolated_candidate(
        root=tmp_path / "sidecar-partial-iso",
        token="test-isolated-sidecar-partial-token",
        transcribe=fixture_transcribe,
        stream_provider=provider,
        fixture_notes=("HTTP FakeEngine + 真实 SidecarStreamProvider，非官方 GPU 引擎",),
        mount_static=False,
    )
    try:
        yield comp
    finally:
        provider.close()
        comp.close()


def _create_session(comp, headers) -> str:
    created = asyncio.run(
        call_asgi(
            comp.app,
            method="POST",
            path="/api/v1/recognize/sessions",
            client=("127.0.0.1", 50000),
            server=(comp.bind_host, comp.bind_port),
            headers={**headers, "content-type": "application/json"},
            body=b'{"language":"zh"}',
        )
    )
    assert created["status"] == 200, created
    return created["body"]["session_id"]


def _post_chunk(comp, headers, sid: str, index: int = 0):
    return asyncio.run(
        call_asgi(
            comp.app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/chunks",
            client=("127.0.0.1", 50000),
            server=(comp.bind_host, comp.bind_port),
            headers={
                **headers,
                SIDECAR_CHUNK_HEADER: str(index),
                "content-type": "application/octet-stream",
            },
            body=PCM,
        )
    )


def _post_finish(comp, headers, sid: str):
    return asyncio.run(
        call_asgi(
            comp.app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/finish",
            client=("127.0.0.1", 50000),
            server=(comp.bind_host, comp.bind_port),
            headers={**headers, "content-type": "application/json"},
            body=b"{}",
        )
    )


def _post_cancel(comp, headers, sid: str):
    return asyncio.run(
        call_asgi(
            comp.app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/cancel",
            client=("127.0.0.1", 50000),
            server=(comp.bind_host, comp.bind_port),
            headers={**headers, "content-type": "application/json"},
            body=b"{}",
        )
    )


def _submit(comp, headers, sid: str, job_id: str):
    import json

    return asyncio.run(
        call_asgi(
            comp.app,
            method="POST",
            path="/api/v1/utterances",
            client=("127.0.0.1", 50000),
            server=(comp.bind_host, comp.bind_port),
            headers={**headers, "content-type": "application/json"},
            body=json.dumps({"job_id": job_id, "session_id": sid, "source": "desktop", "focus_id": "f"}).encode(),
        )
    )


def test_http_fake_sidecar_partial_reaches_app_sse_before_finish(tmp_path: Path):
    with fake_sidecar(engine=FakeEngine(text="sidecar-fixture-final")) as (engine, server, client, provider):
        assert type(provider).__name__ == "SidecarStreamProvider"
        with isolated_sidecar_app(tmp_path, provider) as comp:
            headers = _app_headers(comp)
            sid = _create_session(comp, headers)
            pump = start_app_sse(
                comp.app,
                path=f"/api/v1/recognize/sessions/{sid}/events",
                headers=headers,
                bind=(comp.bind_host, comp.bind_port),
            )
            try:
                chunk = _post_chunk(comp, headers, sid)
                assert chunk["status"] == 202, chunk
                before = pump.wait_partials(1)
                assert "sidecar-fixture-final" in before
                assert "final" not in pump.kinds()
                kinds_before_finish = pump.kinds()
                finish = _post_finish(comp, headers, sid)
                assert finish["status"] == 200, finish
                pump.wait_closed()
                kinds = pump.kinds()
                assert kinds_before_finish, "finish 前应用 SSE 不得为空"
                assert "partial" in kinds_before_finish
                assert "final" not in kinds_before_finish
                assert kinds.count("final") == 1
                assert kinds.count("closed") == 1
                assert kinds.index("partial") < kinds.index("final") < kinds.index("closed")
                final = next(item for item in pump.snapshot() if item.get("event") == "final")
                assert final["text"] == "sidecar-fixture-final"
                submitted = _submit(comp, headers, sid, "job-partial-b1")
                assert submitted["status"] == 200, submitted
                assert submitted["body"]["text"] == "sidecar-fixture-final"
            finally:
                stop_app_sse(pump)
        assert engine.finished
        assert not engine.streams


def test_zero_partials_then_single_final(tmp_path: Path):
    engine = FakeEngine(text="only-final", partials_per_chunk=0)
    with fake_sidecar(engine=engine) as (_eng, _server, _client, provider):
        with isolated_sidecar_app(tmp_path, provider) as comp:
            headers = _app_headers(comp)
            sid = _create_session(comp, headers)
            pump = start_app_sse(
                comp.app,
                path=f"/api/v1/recognize/sessions/{sid}/events",
                headers=headers,
                bind=(comp.bind_host, comp.bind_port),
            )
            try:
                chunk = _post_chunk(comp, headers, sid)
                assert chunk["status"] == 202, chunk
                time.sleep(0.15)
                assert pump.kinds() == []
                finish = _post_finish(comp, headers, sid)
                assert finish["status"] == 200, finish
                pump.wait_closed()
                assert pump.tails() == []
                kinds = pump.kinds()
                assert kinds == ["final", "closed"], kinds
                assert pump.snapshot()[0]["text"] == "only-final"
            finally:
                stop_app_sse(pump)


def test_many_partials_from_one_chunk_preserve_order(tmp_path: Path):
    tails = ["alpha-tail", "beta-tail", "gamma-tail"]
    engine = FakeEngine(text="many-final", partials_per_chunk=3, partial_tails=tails)
    with fake_sidecar(engine=engine) as (_eng, _server, _client, provider):
        with isolated_sidecar_app(tmp_path, provider) as comp:
            headers = _app_headers(comp)
            sid = _create_session(comp, headers)
            pump = start_app_sse(
                comp.app,
                path=f"/api/v1/recognize/sessions/{sid}/events",
                headers=headers,
                bind=(comp.bind_host, comp.bind_port),
            )
            try:
                chunk = _post_chunk(comp, headers, sid)
                assert chunk["status"] == 202, chunk
                got = pump.wait_partials(3)
                assert got == tails
                assert "final" not in pump.kinds()
                finish = _post_finish(comp, headers, sid)
                assert finish["status"] == 200, finish
                pump.wait_closed()
                kinds = pump.kinds()
                assert kinds[:3] == ["partial", "partial", "partial"]
                assert kinds[3:] == ["final", "closed"]
                assert pump.tails() == tails
                assert kinds.count("final") == 1
                assert pump.snapshot()[3]["text"] == "many-final"
            finally:
                stop_app_sse(pump)


def test_cancel_does_not_submit_and_stops_reader(tmp_path: Path):
    with fake_sidecar(engine=FakeEngine(text="should-not-submit")) as (engine, _server, _client, provider):
        with isolated_sidecar_app(tmp_path, provider) as comp:
            headers = _app_headers(comp)
            sid = _create_session(comp, headers)
            pump = start_app_sse(
                comp.app,
                path=f"/api/v1/recognize/sessions/{sid}/events",
                headers=headers,
                bind=(comp.bind_host, comp.bind_port),
            )
            try:
                _post_chunk(comp, headers, sid)
                pump.wait_partials(1)
                cancelled = _post_cancel(comp, headers, sid)
                assert cancelled["status"] == 200, cancelled
                assert cancelled["body"]["reason"] == "cancelled"
                pump.wait_closed()
                kinds = pump.kinds()
                assert "final" not in kinds
                assert kinds[-1] == "closed"
                submitted = _submit(comp, headers, sid, "job-cancelled")
                assert submitted["status"] in {409, 400, 422, 404}, submitted
                jobs = list(comp.runtime.jobs._store.list_jobs())
                assert jobs == []
                snap = comp.runtime.finals.lookup(sid)
                assert snap is not None
                assert snap.closed_reason == SessionClosedReason.cancelled
            finally:
                stop_app_sse(pump)
        assert sid in engine.cancelled or engine.cancelled
        assert not engine.finished or sid not in engine.finished
        assert not _leftover_reader_threads()


def test_disconnect_does_not_enqueue_and_stops_reader(tmp_path: Path):
    from tests.api.asgi_client import collect_sse

    with fake_sidecar(engine=FakeEngine(text="should-not-enqueue")) as (_engine, _server, _client, provider):
        with isolated_sidecar_app(tmp_path, provider) as comp:
            headers = _app_headers(comp)

            # 0 事件即断开（与既有 API SSE 探针相同路径）
            sid0 = _create_session(comp, headers)
            result0 = asyncio.run(
                collect_sse(
                    comp.app,
                    path=f"/api/v1/recognize/sessions/{sid0}/events",
                    client=("127.0.0.1", 50000),
                    headers=headers,
                    server=(comp.bind_host, comp.bind_port),
                    disconnect_after_events=0,
                    timeout=2.0,
                )
            )
            snap0 = comp.runtime.finals.lookup(sid0)
            assert snap0 is not None, result0
            assert snap0.closed_reason == SessionClosedReason.disconnected
            finish0 = _post_finish(comp, headers, sid0)
            assert finish0["status"] == 409, finish0
            assert not _leftover_reader_threads()

            # mid-stream 收到非空 partial 后再断开：不断稿、不入队
            sid = _create_session(comp, headers)
            started = threading.Event()

            def _after_start() -> None:
                started.set()

            def _post() -> None:
                if not started.wait(timeout=5.0):
                    raise AssertionError("session SSE 未就绪")
                _post_chunk(comp, headers, sid)

            worker = threading.Thread(target=_post, name="post-chunk-before-disconnect", daemon=True)
            worker.start()
            result = asyncio.run(
                collect_sse(
                    comp.app,
                    path=f"/api/v1/recognize/sessions/{sid}/events",
                    client=("127.0.0.1", 50000),
                    headers=headers,
                    server=(comp.bind_host, comp.bind_port),
                    after_start=_after_start,
                    disconnect_after_events=1,
                    timeout=5.0,
                )
            )
            worker.join(timeout=5.0)
            events = result["events"]
            kinds = [item.get("event") for item in events]
            assert "partial" in kinds, events
            assert any(item.get("tail_text") for item in events if item.get("event") == "partial"), events
            assert "final" not in kinds
            snap = comp.runtime.finals.lookup(sid)
            assert snap is not None
            assert snap.closed_reason == SessionClosedReason.disconnected
            finish = _post_finish(comp, headers, sid)
            assert finish["status"] == 409, finish
            submitted = _submit(comp, headers, sid, "job-disconnected")
            assert submitted["status"] in {409, 400, 422, 404}, submitted
            jobs = list(comp.runtime.jobs._store.list_jobs())
            assert jobs == []
            assert not _leftover_reader_threads()



def test_reader_error_visible_when_sidecar_drops(tmp_path: Path):
    with fake_sidecar(engine=FakeEngine(text="drop-me")) as (_eng, server, _client, provider):
        with isolated_sidecar_app(tmp_path, provider) as comp:
            headers = _app_headers(comp)
            sid = _create_session(comp, headers)
            pump = start_app_sse(
                comp.app,
                path=f"/api/v1/recognize/sessions/{sid}/events",
                headers=headers,
                bind=(comp.bind_host, comp.bind_port),
            )
            try:
                _post_chunk(comp, headers, sid)
                pump.wait_partials(1)
                server.close()
                pump.wait_closed(timeout=8.0)
                kinds = pump.kinds()
                assert "error" in kinds or "closed" in kinds, kinds
                if "closed" in kinds:
                    closed = next(item for item in pump.snapshot() if item.get("event") == "closed")
                    assert closed["reason"] in {"error", "disconnected", "cancelled"}
                submitted = _submit(comp, headers, sid, "job-dropped")
                assert submitted["status"] in {409, 400, 422, 404, 503}, submitted
                jobs = list(comp.runtime.jobs._store.list_jobs())
                assert jobs == []
            finally:
                stop_app_sse(pump)
        assert not _leftover_reader_threads()


def test_this_module_does_not_import_fixture_stream_provider():
    import ast
    import tests.integration.test_sidecar_provider_partials as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[-1])
    assert "FixtureStreamProvider" not in imported
    assert "SidecarStreamProvider" in imported
    assert "FixtureStreamProvider" not in vars(mod)
