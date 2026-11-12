"""生产运行时组合：显式路径 + 显式令牌 + 真实能力适配器。

只有生产入口（同时传 live=True 的 ProductionPaths 与已读取的令牌）才会绑定 live 文件；
测试用隔离根目录 + 假令牌 + 假传输/假进程装配同一函数，验证接线而不碰用户数据。
组合完成后由 activation_blockers() 按验收门闩决定能否正式对用户启用。
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from _asr import AsrSidecarClient
from _asr_provider import SidecarStreamProvider
from _config import ConfigStore, migrate_to_product_config
from _contracts import (
    PRODUCT_ASR_MODEL,
    SIDECAR_DEFAULT_BASE_URL,
    ErrorCode,
    LogicError,
    PolishRequest,
    PolishResponse,
    SetupProgress,
    SetupTaskKind,
    TranslateRequest,
    TranslateResponse,
)
from _events import EventBus
from _hotkey import HotkeyBindings
from _history import HistoryStore
from _history_service import HistoryService
from _jobs import JobStore
from _jobs_service import JobsService
from _linux.conflicts import scan_hotkey_conflicts
from _ocr import image_to_text
from _ollama import DEFAULT_OLLAMA_BASE_URL, OllamaClient
from _polish import polish_text
from _session_finals import SessionFinalIndex
from _translate import translate_text
from _utterance import UtteranceService
from _v3.auth import SERVER_PORT, AuthSettings
from _v3.lan_access import LanAccessService, list_physical_lan_ipv4
from _v3.runtime import Runtime, UnmetRuntimeGates, production_unmet_gates
from _v3.setup_actions import (
    asr_download_action,
    ollama_install_action,
    ollama_pull_action,
    systemd_unit_state,
    systemd_user_start_action,
)
from _v3.setup_probe import (
    SidecarProbe,
    dbus_portal_version,
    desktop_daemon_capability,
    hotkey_conflicts_capability,
    probe_asr_sidecar,
    probe_asr_weights,
    probe_capabilities,
    probe_ollama,
    session_type,
)
from _v3.setup_service import SetupProbes, SetupService
from _v3.setup_tasks import ProgressFn, SetupAction, SetupTaskManager, Spawn, thread_spawn

CODE_ROOT = Path(__file__).resolve().parents[1]
ASR_DOWNLOAD_WORKER = CODE_ROOT / "_v3" / "asr_download_worker.py"
STATIC_V3 = CODE_ROOT / "static" / "v3"


@dataclass(frozen=True)
class ProductionPaths:
    """生产数据位置的唯一表。live=False 时拒绝指向项目真实数据根。"""

    data_root: Path
    config: Path
    history: Path
    jobs: Path
    token: Path
    upload_tmp: Path
    setup_logs: Path
    cert: Path
    key: Path
    asr_model_dir: Path
    asr_python: Path
    hf_home: Path
    ollama_install_dir: Path
    ollama_models: Path
    live: bool

    @classmethod
    def for_root(cls, data_root: Path, *, live: bool) -> "ProductionPaths":
        root = Path(data_root).resolve()
        if not live and root == CODE_ROOT:
            raise RuntimeError("未显式 live 授权时拒绝绑定项目真实数据根")
        runtime_dir = root / ".cache" / "v3-feasibility"
        return cls(
            data_root=root,
            config=root / ".config.json",
            history=root / ".cache" / "history.jsonl",
            jobs=root / ".cache" / "jobs.json",
            token=root / ".web_input_token",
            upload_tmp=root / ".cache" / "upload-tmp",
            setup_logs=root / ".cache" / "setup-logs",
            cert=root / "certs" / "cert.pem",
            key=root / "certs" / "key.pem",
            asr_model_dir=runtime_dir / "models" / "Qwen3-ASR-1.7B",
            asr_python=runtime_dir / "venv-asr" / "bin" / "python",
            hf_home=runtime_dir / "hf-home",
            ollama_install_dir=runtime_dir / "ollama-bin-0.40.16",
            ollama_models=runtime_dir / "ollama-models",
            live=live,
        )

    @property
    def ollama_binary(self) -> Path:
        return self.ollama_install_dir / "bin" / "ollama"


@dataclass(frozen=True)
class ProductionEndpoints:
    port: int = SERVER_PORT
    sidecar_url: str = SIDECAR_DEFAULT_BASE_URL
    ollama_url: str = DEFAULT_OLLAMA_BASE_URL
    ollama_version: str = "0.40.16"
    asr_unit: str = "ollama-voice-asr.service"
    ollama_unit: str = "ollama-voice-ollama.service"
    server_unit: str = "ollama-voice-server.service"
    daemon_unit: str = "ollama-voice-daemon.service"

    @property
    def units(self) -> tuple[str, ...]:
        """四个用户单元（与 _v3.units.UNIT_NAMES 同集合，tests/integration 断言）。"""
        return (self.ollama_unit, self.asr_unit, self.server_unit, self.daemon_unit)


@dataclass(frozen=True)
class SystemAdapters:
    """所有会碰系统的边界，生产给真实实现，测试给夹具。"""

    environ: Mapping[str, str]
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    popen: Callable[..., subprocess.Popen] = subprocess.Popen
    which: Callable[[str], str | None] = shutil.which
    portal_version: Callable[[str], tuple[int | None, str | None]] = dbus_portal_version
    ocr_languages: Callable[[], list[str]] | None = None
    list_ips: Callable[[], list[str]] = list_physical_lan_ipv4
    http_transport: httpx.BaseTransport | None = None
    spawn: Spawn = thread_spawn


def _tesseract_languages() -> list[str]:
    import pytesseract

    return list(pytesseract.get_languages(config=""))


def _client(base_url: str, adapters: SystemAdapters, *, timeout: httpx.Timeout | float) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=timeout, transport=adapters.http_transport, follow_redirects=True)


def asr_start_with_preflight(
    *,
    unit: str,
    start: SetupAction,
    sidecar_probe: Callable[[], SidecarProbe],
    run: Callable[..., subprocess.CompletedProcess],
    which: Callable[[str], str | None],
) -> SetupAction:
    """设置页「启动识别服务」：已加载 → 不再 start；单元已在运行/启动中 → 不预检，交给 start 动作按实际状态判定；
    否则先做与 sidecar 同源的显存预检（读单元真实 ExecStart），不足或无法判断 → 失败且不 start。
    读单元状态失败直接失败（未知 ≠ 已在运行）。"""

    def action(report: ProgressFn) -> str:
        probe = sidecar_probe()
        if probe.running and probe.loaded:
            return f"{unit} 已加载，无需启动"
        state = systemd_unit_state(unit, run=run)
        if state.active_state not in ("active", "activating", "reloading"):
            report(SetupProgress(stage="显存预检"))
            # 函数内导入：start.sh 以 `python3 -m _v3.gpu_preflight` 调同一实现，_v3 包初始化会导入本模块；
            # 顶层导入会让 runpy 在执行 __main__ 前已载入该模块并打印 RuntimeWarning 混进启动输出。
            from _v3.gpu_preflight import asr_gpu_preflight_for_unit

            result = asr_gpu_preflight_for_unit(unit, run=run, which=which)
            if not result.ok:
                raise LogicError(
                    ErrorCode.dependency_failed,
                    f"未启动识别服务：{result.reason}",
                    {"unit": unit, "preflight": result.kind, "required_mib": result.required_mib,
                     "free_mib": result.free_mib, "total_mib": result.total_mib},
                )
            report(SetupProgress(stage=result.reason))
        return start(report)

    return action


def _setup_service(
    paths: ProductionPaths,
    endpoints: ProductionEndpoints,
    adapters: SystemAdapters,
    config: ConfigStore,
) -> SetupService:
    def model() -> str:
        return config.load().ollama_model

    def ollama_probe():
        with _client(endpoints.ollama_url, adapters, timeout=3.0) as client:
            return probe_ollama(binary=paths.ollama_binary, client=client, model=model())

    def sidecar_probe():
        with _client(endpoints.sidecar_url, adapters, timeout=3.0) as client:
            return probe_asr_sidecar(client)

    def hotkey_scan():
        cfg = config.load()
        bindings = HotkeyBindings(voice=cfg.hotkey_voice, screenshot=cfg.hotkey_screenshot, repeat=cfg.hotkey_repeat)
        return scan_hotkey_conflicts(bindings, run=adapters.run, which=adapters.which)

    def capabilities(session: str):
        return [
            *probe_capabilities(
                session=session,
                which=adapters.which,
                portal_version=adapters.portal_version,
                ocr_languages=ocr_languages,
            ),
            hotkey_conflicts_capability(hotkey_scan),
            desktop_daemon_capability(
                read_state=lambda unit: systemd_unit_state(unit, run=adapters.run),
                daemon_unit=endpoints.daemon_unit,
                units=endpoints.units,
                unmet_gates=production_unmet_gates,
            ),
        ]

    ocr_languages = adapters.ocr_languages or _tesseract_languages
    probes = SetupProbes(
        session=lambda: session_type(adapters.environ),
        ollama=ollama_probe,
        asr_weights=lambda: probe_asr_weights(paths.asr_model_dir),
        asr_sidecar=sidecar_probe,
        capabilities=capabilities,
    )
    download_env = dict(adapters.environ)
    download_env.update({"HF_HOME": str(paths.hf_home), "HF_HUB_DISABLE_PROGRESS_BARS": "1"})
    long_read = httpx.Timeout(30.0, read=None)
    actions = {
        SetupTaskKind.ollama_pull: lambda report: ollama_pull_action(
            client_factory=lambda: _client(endpoints.ollama_url, adapters, timeout=long_read), model=model()
        )(report),
        SetupTaskKind.ollama_install: ollama_install_action(
            version=endpoints.ollama_version,
            install_dir=paths.ollama_install_dir,
            client_factory=lambda: httpx.Client(timeout=long_read, transport=adapters.http_transport, follow_redirects=True),
            run=adapters.run,
        ),
        SetupTaskKind.ollama_start: systemd_user_start_action(
            unit=endpoints.ollama_unit, ready=lambda: ollama_probe().running, run=adapters.run, timeout_s=60.0
        ),
        SetupTaskKind.asr_start: asr_start_with_preflight(
            unit=endpoints.asr_unit,
            start=systemd_user_start_action(
                unit=endpoints.asr_unit,
                ready=lambda: (lambda p: p.running and p.loaded)(sidecar_probe()),
                run=adapters.run,
                timeout_s=300.0,
            ),
            sidecar_probe=sidecar_probe,
            run=adapters.run,
            which=adapters.which,
        ),
        SetupTaskKind.asr_download: asr_download_action(
            python=paths.asr_python,
            worker=ASR_DOWNLOAD_WORKER,
            repo_id=PRODUCT_ASR_MODEL,
            model_dir=paths.asr_model_dir,
            log_path=paths.setup_logs / "asr-download.log",
            env=download_env,
            popen=adapters.popen,
        ),
    }
    return SetupService(
        probes=probes,
        tasks=SetupTaskManager(actions, spawn=adapters.spawn),
        unmet_gates=lambda: list(production_unmet_gates()),
    )


def compose_production_runtime(
    *,
    paths: ProductionPaths,
    token: str,
    adapters: SystemAdapters,
    endpoints: ProductionEndpoints = ProductionEndpoints(),
) -> Runtime:
    """组合生产运行时并执行启动恢复（claimed → delivery_unknown，不重投）。不启动 HTTP 服务。"""
    if not isinstance(paths, ProductionPaths):
        raise TypeError("必须显式传入 ProductionPaths")
    if not token or not token.strip():
        raise ValueError("必须显式传入访问令牌")
    paths.upload_tmp.mkdir(parents=True, exist_ok=True)
    config = ConfigStore(paths.config)
    migrate_to_product_config(config)
    events = EventBus()
    history = HistoryService(HistoryStore(paths.history), environ=adapters.environ)
    jobs = JobsService(JobStore(paths.jobs), events, claim_lease_s=60.0)
    jobs.recover()
    finals = SessionFinalIndex()

    asr = AsrSidecarClient(base_url=endpoints.sidecar_url, transport=adapters.http_transport)
    ollama = OllamaClient(base_url=endpoints.ollama_url, transport=adapters.http_transport)

    def polish(request: PolishRequest, *, model: str | None = None) -> PolishResponse:
        return polish_text(request, model=model or config.load().ollama_model, client=ollama)

    def translate(request: TranslateRequest) -> TranslateResponse:
        return translate_text(request, model=config.load().ollama_model, client=ollama)

    utterance = UtteranceService(
        jobs=jobs,
        history=history,
        events=events,
        config=config,
        finals=finals,
        polish=polish,
        transcribe=asr.transcribe,
    )
    return Runtime(
        config=config,
        history=history,
        jobs=jobs,
        events=events,
        finals=finals,
        utterance=utterance,
        auth=AuthSettings(token=token, port=endpoints.port),
        transcribe=asr.transcribe,
        polish=polish,
        translate=translate,
        ocr=image_to_text,
        stream_provider=SidecarStreamProvider(asr),
        setup=_setup_service(paths, endpoints, adapters, config),
        lan=LanAccessService(token=token, list_ips=adapters.list_ips, port=endpoints.port),
        unmet_gates=production_unmet_gates(),
        upload_tmp=paths.upload_tmp,
    )


def activation_blockers(runtime: Runtime) -> tuple[str, ...]:
    """正式对用户启用前必须为空。"""
    return tuple(runtime.unmet_gates)


def require_activation_allowed(runtime: Runtime) -> None:
    blockers = activation_blockers(runtime)
    if blockers:
        raise UnmetRuntimeGates(blockers)


# 验收运行：门闩未清时仍可由用户手动启动以做真机检测；status/setup 照常显示全部未达门闩。
# 只认显式环境变量（仅 ./start.sh --acceptance 写入单元 drop-in），常量见 _activation。
from _activation import ACCEPTANCE_ENV  # noqa: E402


def build_production_app(
    *,
    data_root: Path = CODE_ROOT,
    environ: Mapping[str, str],
    static_v3: Path = STATIC_V3,
):
    """唯一生产装配：live 路径 + 读/建令牌 + 真实适配器 + 门闩判定 + 唯一界面 static/v3。"""
    from _v3.lan_access import load_or_create_token
    from _v3_app import create_app

    paths = ProductionPaths.for_root(data_root, live=True)
    token = load_or_create_token(paths.token)
    adapters = SystemAdapters(environ=environ, ocr_languages=_tesseract_languages)
    runtime = compose_production_runtime(paths=paths, token=token, adapters=adapters)
    if environ.get(ACCEPTANCE_ENV) != "1":
        require_activation_allowed(runtime)
    return create_app(runtime, static_v3=static_v3), paths
