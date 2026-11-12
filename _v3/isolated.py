"""显式隔离候选组合入口。复用 build_sandbox_runtime + create_app，不另建业务栈。

调用方必须传入隔离 root、测试 token，以及（可选）证书、回环服务地址、已构造的能力。
不读取 live 配置/队列/历史/token，不绑定 17945/17946/11434，不把夹具当成生产。
compose_production_runtime 仍保持未达标门闩，本入口不得绕过或宣称生产就绪。
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from _asr import AsrSidecarClient
from _asr_provider import SidecarStreamProvider
from _contracts import PolishRequest, PolishResponse, TranslateRequest, TranslateResponse
from _jobs_client import JobsClient
from _ollama import OllamaClient
from _polish import polish_text
from _translate import translate_text
from _v3.runtime import (
    PRODUCTION_UNMET_GATES,
    Runtime,
    StreamingAsrProvider,
    TranscribeFn,
    UnmetRuntimeGates,
    assert_sandbox_path,
    build_sandbox_runtime,
    production_unmet_gates,
)
from _v3.production import STATIC_V3, compose_production_runtime
from _v3.setup_service import SetupService
from _v3_app import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PORTS = frozenset({17945, 17946, 11434})
ISOLATED_UNMET_GATES: tuple[str, ...] = (
    "isolated candidate composition: 非生产，未切流，夹具不得当成官方引擎。",
) + PRODUCTION_UNMET_GATES


def allocate_loopback_port(*, host: str = "127.0.0.1") -> int:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("隔离组合只允许回环地址")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host if host != "localhost" else "127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    if port in FORBIDDEN_PORTS:
        return allocate_loopback_port(host=host)
    return port


def _port_of(url: str) -> int | None:
    parsed = urlparse(url)
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


def _require_isolated_url(url: str, *, name: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{name} 必须是回环地址")
    port = _port_of(url)
    if port in FORBIDDEN_PORTS:
        raise ValueError(f"{name} 不得使用保留端口 {sorted(FORBIDDEN_PORTS)}")
    return url


@dataclass
class IsolatedComposition:
    """一次隔离组装的结果。http_server 仅在显式 start_loopback_https 后存在。"""

    runtime: Runtime
    app: FastAPI
    root: Path
    token: str
    bind_host: str
    bind_port: int
    cert: Path | None
    key: Path | None
    sidecar_base_url: str | None
    ollama_base_url: str | None
    unmet_gates: tuple[str, ...]
    static_mounted: bool
    fixture_notes: tuple[str, ...]
    jobs_client: JobsClient
    http_server: Any = None
    _test_client: TestClient | None = field(default=None, repr=False)

    def base_url(self) -> str:
        return f"https://{self.bind_host}:{self.bind_port}"

    def local_headers(self) -> dict[str, str]:
        return {"Origin": self.base_url(), "Host": f"{self.bind_host}:{self.bind_port}"}

    def test_client(self) -> TestClient:
        if self._test_client is None:
            self._test_client = TestClient(
                self.app,
                client=("127.0.0.1", 50000),
                base_url=self.base_url(),
            )
        return self._test_client

    def start_loopback_https(self) -> str:
        """在已分配的回环端口上启动 HTTPS，供可选的真实套接字检查。不是生产 daemon。"""
        if self.http_server is not None:
            return self.base_url()
        if self.cert is None or self.key is None or not Path(self.cert).is_file() or not Path(self.key).is_file():
            raise RuntimeError("启动隔离 HTTPS 需要显式项目证书与私钥")
        if self.bind_port in FORBIDDEN_PORTS:
            raise RuntimeError(f"拒绝在保留端口监听: {self.bind_port}")
        import threading
        import time
        import uvicorn

        config = uvicorn.Config(
            self.app,
            host=self.bind_host,
            port=self.bind_port,
            ssl_certfile=str(self.cert),
            ssl_keyfile=str(self.key),
            log_level="warning",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="isolated-v3-https", daemon=True)
        thread.start()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if getattr(server, "started", False):
                self.http_server = server
                return self.base_url()
            time.sleep(0.05)
        raise RuntimeError("隔离 HTTPS 未在时限内 started，拒绝写 ready")

    def close(self) -> None:
        server = self.http_server
        if server is not None:
            server.should_exit = True
            if hasattr(server, "force_exit"):
                server.force_exit = True
            self.http_server = None
        if self._test_client is not None:
            self._test_client.close()
            self._test_client = None
        self.jobs_client.close()

    def __enter__(self) -> "IsolatedComposition":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def compose_isolated_candidate(
    *,
    root: Path,
    token: str,
    cert: Path | None = None,
    key: Path | None = None,
    bind_host: str = "127.0.0.1",
    bind_port: int | None = None,
    sidecar_base_url: str | None = None,
    sidecar_client: AsrSidecarClient | None = None,
    stream_provider: StreamingAsrProvider | None = None,
    transcribe: TranscribeFn | None = None,
    polish: Callable[[PolishRequest], PolishResponse] | None = None,
    translate: Callable[[TranslateRequest], TranslateResponse] | None = None,
    ocr: Callable[..., str] | None = None,
    ollama_base_url: str | None = None,
    ollama_model: str = "qwen3.5:4b",
    ollama_client: OllamaClient | None = None,
    mount_static: bool = True,
    setup: SetupService | None = None,
    lan_ips: Callable[[], list[str]] | None = None,
    unmet_gates: tuple[str, ...] | None = None,
    fixture_notes: tuple[str, ...] = (),
) -> IsolatedComposition:
    """组装隔离候选。必须由调用方显式传入隔离根与 token；不回落 live，不启 GPU。"""
    if bind_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("隔离组合只允许绑定回环")
    if bind_port is None:
        bind_port = allocate_loopback_port(host="127.0.0.1" if bind_host == "localhost" else bind_host)
    if bind_port in FORBIDDEN_PORTS:
        raise ValueError(f"隔离组合不得使用保留端口 {sorted(FORBIDDEN_PORTS)}")
    if cert is not None:
        cert = Path(cert)
        if not cert.is_file():
            raise ValueError(f"证书不存在: {cert}")
    if key is not None:
        key = Path(key)
        if not key.is_file():
            raise ValueError(f"私钥不存在: {key}")

    if sidecar_base_url is not None:
        sidecar_base_url = _require_isolated_url(sidecar_base_url, name="sidecar_base_url")
    if ollama_base_url is not None:
        ollama_base_url = _require_isolated_url(ollama_base_url, name="ollama_base_url")

    notes = list(fixture_notes)
    if stream_provider is None and sidecar_client is not None:
        stream_provider = SidecarStreamProvider(sidecar_client)
        notes.append("stream_provider=SidecarStreamProvider(显式 sidecar_client)，不是官方引擎默认值")
    elif stream_provider is None and sidecar_base_url is not None:
        stream_provider = SidecarStreamProvider(AsrSidecarClient(base_url=sidecar_base_url))
        notes.append("stream_provider=SidecarStreamProvider(显式 sidecar_base_url)，不是官方引擎默认值")
    elif stream_provider is not None:
        notes.append("stream_provider 由调用方显式注入")
    else:
        notes.append("未注入 stream_provider：识别会话应不可用，不得假装成功")

    if polish is None and (ollama_base_url is not None or ollama_client is not None):
        if ollama_client is None and ollama_base_url is None:
            raise ValueError("不允许默认落到 11434")
        base = ollama_base_url

        def polish(request: PolishRequest, _base=base, _client=ollama_client, _model=ollama_model) -> PolishResponse:
            return polish_text(request, model=_model, client=_client, base_url=_base or "http://127.0.0.1:1")

        notes.append("polish 复用 _polish.polish_text，地址由调用方显式提供")
    if translate is None and (ollama_base_url is not None or ollama_client is not None):
        base = ollama_base_url

        def translate(request: TranslateRequest, _base=base, _client=ollama_client, _model=ollama_model) -> TranslateResponse:
            return translate_text(request, model=_model, client=_client, base_url=_base or "http://127.0.0.1:1")

        notes.append("translate 复用 _translate.translate_text，地址由调用方显式提供")

    gates = tuple(unmet_gates or ISOLATED_UNMET_GATES)
    if not any("未切流" in item or "尚未挂入生产" in item for item in gates):
        gates = gates + PRODUCTION_UNMET_GATES
    # 始终附上生产门闩，避免候选入口假装生产就绪
    for item in production_unmet_gates():
        if item not in gates:
            gates = gates + (item,)

    runtime = build_sandbox_runtime(
        root=assert_sandbox_path(Path(root)),
        token=token,
        transcribe=transcribe,
        polish=polish,
        translate=translate,
        ocr=ocr,
        stream_provider=stream_provider,
        setup=setup,
        lan_ips=lan_ips,
        unmet_gates=gates,
        port=bind_port,
    )
    static_mounted = bool(mount_static)
    app = create_app(runtime, static_v3=STATIC_V3 if static_mounted else None)
    if static_mounted:
        notes.append("挂载唯一界面 static/v3（与生产同一 install_pages），未挂旧 static/")

    test_client = TestClient(app, client=("127.0.0.1", 50000), base_url=f"https://{bind_host}:{bind_port}")
    jobs = JobsClient(base_url=f"https://{bind_host}:{bind_port}", client=test_client)
    return IsolatedComposition(
        runtime=runtime,
        app=app,
        root=Path(root).resolve(),
        token=token,
        bind_host=bind_host,
        bind_port=bind_port,
        cert=cert,
        key=key,
        sidecar_base_url=sidecar_base_url,
        ollama_base_url=ollama_base_url,
        unmet_gates=runtime.unmet_gates,
        static_mounted=static_mounted,
        fixture_notes=tuple(notes),
        jobs_client=jobs,
        _test_client=test_client,
    )


# 再导出生产入口，便于测试证明候选没有替换它
__all__ = [
    "FORBIDDEN_PORTS",
    "ISOLATED_UNMET_GATES",
    "IsolatedComposition",
    "allocate_loopback_port",
    "compose_isolated_candidate",
    "compose_production_runtime",
    "production_unmet_gates",
    "UnmetRuntimeGates",
]
