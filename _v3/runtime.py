"""显式运行时依赖与沙箱组合。禁止无参默认指向 live 配置/历史/队列。

生产组合在 _v3/production.py，必须显式给出路径与令牌。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from _activation import PRODUCTION_UNMET_GATES
from _config import ConfigStore, default_config_path
from _contracts import (
    AppConfig,
    PolishRequest,
    PolishResponse,
    SttResponse,
    TranslateRequest,
    TranslateResponse,
)
from _events import EventBus
from _history import HistoryStore
from _history_service import HistoryService
from _jobs import JobStore, default_jobs_path
from _jobs_service import JobsService
from _session_finals import SessionFinalIndex
from _utterance import UtteranceService
from _v3.auth import AuthSettings
from _v3.lan_access import LanAccessService
from _v3.sessions import StreamingAsrProvider
from _v3.setup_service import SetupService

TranscribeFn = Callable[..., SttResponse]
PolishFn = Callable[[PolishRequest], PolishResponse]
TranslateFn = Callable[[TranslateRequest], TranslateResponse]
OcrFn = Callable[..., str]


class UnmetRuntimeGates(RuntimeError):
    """生产组合未达标。不得当成已可切流。"""

    def __init__(self, gates: tuple[str, ...]):
        self.gates = gates
        super().__init__("；".join(gates))


# 官方/真机门闩常量唯一来源在 _activation（入口脚本、单元 ExecCondition、daemon、Web 工厂共用）。


def production_unmet_gates() -> tuple[str, ...]:
    """只在对应验收证据关闭后由代码变更减少条目；不得因软件接线完成而清空官方项。"""
    return PRODUCTION_UNMET_GATES


def effective_unmet_gates(runtime: "Runtime") -> list[str]:
    """运行时自带门闩 + 生产门闩，去重保序。status 与 setup/check 共用这一份。"""
    merged: list[str] = []
    for item in tuple(runtime.unmet_gates) + production_unmet_gates():
        if item not in merged:
            merged.append(item)
    return merged


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def assert_sandbox_path(path: Path) -> Path:
    resolved = path.resolve()
    project = _project_root()
    forbidden = {
        default_config_path().resolve(),
        default_jobs_path().resolve(),
        (project / ".cache" / "history.jsonl").resolve(),
        (project / ".config.json").resolve(),
        (project / ".web_input_token").resolve(),
    }
    if resolved in forbidden:
        raise RuntimeError(f"拒绝绑定 live 路径: {resolved}")
    cache = (project / ".cache").resolve()
    if resolved == cache or resolved.is_relative_to(cache):
        raise RuntimeError(f"拒绝绑定 live 缓存: {resolved}")
    return resolved


@dataclass
class Runtime:
    config: ConfigStore
    history: HistoryService
    jobs: JobsService
    events: EventBus
    finals: SessionFinalIndex
    utterance: UtteranceService
    auth: AuthSettings
    transcribe: TranscribeFn | None = None
    polish: PolishFn | None = None
    translate: TranslateFn | None = None
    ocr: OcrFn | None = None
    stream_provider: StreamingAsrProvider | None = None
    setup: SetupService | None = None
    lan: LanAccessService | None = None
    unmet_gates: tuple[str, ...] = field(default_factory=tuple)
    max_upload_bytes: int = 15 * 1024 * 1024
    max_chunk_bytes: int = 160_000
    max_session_pcm_bytes: int = 16_000 * 2 * 120
    upload_tmp: Path | None = None
    sse_queue_max: int = 32
    session_retain_closed_s: float = 60.0
    session_idle_open_s: float = 180.0
    clock: Callable[[], float] = time.monotonic


def build_sandbox_runtime(
    *,
    root: Path,
    token: str,
    transcribe: TranscribeFn | None = None,
    polish: PolishFn | None = None,
    translate: TranslateFn | None = None,
    ocr: OcrFn | None = None,
    stream_provider: StreamingAsrProvider | None = None,
    setup: SetupService | None = None,
    lan_ips: Callable[[], list[str]] | None = None,
    unmet_gates: tuple[str, ...] = ("sandbox runtime: 非生产，未切流",),
    port: int = 17945,
    polish_enabled: bool = False,
    max_upload_bytes: int | None = None,
    max_chunk_bytes: int | None = None,
    sse_queue_max: int | None = None,
    session_retain_closed_s: float | None = None,
    session_idle_open_s: float | None = None,
    clock: Callable[[], float] | None = None,
) -> Runtime:
    """测试/闸门专用。必须给出 sandbox 根目录与假 token，不会回落到 live 路径。"""
    if not token or not str(token).strip():
        raise ValueError("sandbox 必须显式提供测试 token，且不得读取 live token")
    lowered = str(token).lower()
    if "live" in lowered or lowered.startswith("prod"):
        raise ValueError("拒绝看起来像生产用途的 token 名称")
    root = assert_sandbox_path(Path(root))
    root.mkdir(parents=True, exist_ok=True)
    config = ConfigStore(assert_sandbox_path(root / "config.json"))
    config.save(AppConfig(ollama_model="qwen3.5:4b", polish_enabled=polish_enabled))
    events = EventBus()
    history = HistoryService(HistoryStore(assert_sandbox_path(root / "history.jsonl")), environ={})
    jobs = JobsService(JobStore(assert_sandbox_path(root / "jobs.json")), events, claim_lease_s=60.0)
    finals = SessionFinalIndex()
    utterance = UtteranceService(
        jobs=jobs,
        history=history,
        events=events,
        config=config,
        finals=finals,
        polish=polish,
        transcribe=transcribe,
    )
    upload_tmp = assert_sandbox_path(root / "upload-tmp")
    upload_tmp.mkdir(parents=True, exist_ok=True)
    return Runtime(
        config=config,
        history=history,
        jobs=jobs,
        events=events,
        finals=finals,
        utterance=utterance,
        auth=AuthSettings(token=token, port=port),
        transcribe=transcribe,
        polish=polish,
        translate=translate,
        ocr=ocr,
        stream_provider=stream_provider,
        setup=setup,
        lan=None if lan_ips is None else LanAccessService(token=token, list_ips=lan_ips, port=port),
        unmet_gates=tuple(unmet_gates),
        max_upload_bytes=15 * 1024 * 1024 if max_upload_bytes is None else max_upload_bytes,
        max_chunk_bytes=160_000 if max_chunk_bytes is None else max_chunk_bytes,
        upload_tmp=upload_tmp,
        sse_queue_max=32 if sse_queue_max is None else sse_queue_max,
        session_retain_closed_s=60.0 if session_retain_closed_s is None else session_retain_closed_s,
        session_idle_open_s=180.0 if session_idle_open_s is None else session_idle_open_s,
        clock=time.monotonic if clock is None else clock,
    )
