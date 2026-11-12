"""设置页业务：把真实探测与作业状态组合成 SetupCheckResponse，并决定下一步动作。

就绪只来自探测事实；作业 succeeded 也要以复查后的探测为准。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from _contracts import (
    AsrSetupStatus,
    OllamaSetupStatus,
    SetupCapability,
    SetupCheckResponse,
    SetupComponentState,
    SetupTask,
    SetupTaskKind,
    SetupTaskState,
)
from _v3.setup_probe import AsrWeightsProbe, OllamaProbe, SidecarProbe
from _v3.setup_tasks import SetupTaskManager


def size_label(num_bytes: int | None) -> str | None:
    if not num_bytes:
        return None
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return None


@dataclass(frozen=True)
class SetupProbes:
    """全部只读探测，由组合入口显式注入。"""

    session: Callable[[], str]
    ollama: Callable[[], OllamaProbe]
    asr_weights: Callable[[], AsrWeightsProbe]
    asr_sidecar: Callable[[], SidecarProbe]
    capabilities: Callable[[str], list[SetupCapability]]


def _running(task: SetupTask | None) -> bool:
    return task is not None and task.state == SetupTaskState.running


def _failed(task: SetupTask | None) -> bool:
    return task is not None and task.state == SetupTaskState.failed


class SetupService:
    def __init__(self, *, probes: SetupProbes, tasks: SetupTaskManager, unmet_gates: Callable[[], list[str]]) -> None:
        self._probes = probes
        self.tasks = tasks
        self._unmet_gates = unmet_gates

    def check(self) -> SetupCheckResponse:
        session = self._probes.session()
        return SetupCheckResponse(
            session_type=session,
            asr=self._asr_status(),
            ollama=self._ollama_status(),
            capabilities=self._probes.capabilities(session),
            unmet_gates=self._unmet_gates(),
        )

    def _asr_status(self) -> AsrSetupStatus:
        weights = self._probes.asr_weights()
        sidecar = self._probes.asr_sidecar()
        download = self.tasks.latest(SetupTaskKind.asr_download)
        start = self.tasks.latest(SetupTaskKind.asr_start)
        running = sidecar.running and sidecar.loaded
        common = dict(
            weights_present=weights.complete,
            sidecar_running=running,
            size_label=size_label(weights.bytes_on_disk),
        )
        if _running(download):
            return AsrSetupStatus(status=SetupComponentState.downloading, progress=download.progress,
                                  message=download.message, task_id=download.task_id, **common)
        if not weights.complete:
            if _failed(download):
                return AsrSetupStatus(status=SetupComponentState.failed, message=download.message,
                                      action=SetupTaskKind.asr_download, task_id=download.task_id, **common)
            return AsrSetupStatus(status=SetupComponentState.missing,
                                  message="识别模型未下载完整：" + "、".join(weights.missing),
                                  action=SetupTaskKind.asr_download, **common)
        # 权重齐全：status 只表示权重（ready）；运行与否只看 sidecar_running（=running∧loaded），message 按实时事实区分。
        ready = SetupComponentState.ready
        if running:
            return AsrSetupStatus(status=ready, message="识别服务已加载", **common)
        if _running(start):
            return AsrSetupStatus(status=ready, message=start.message or "正在启动识别服务",
                                  task_id=start.task_id, **common)
        if sidecar.running:
            # 进程已响应但模型未加载完：不再给启动动作，避免重复加载
            return AsrSetupStatus(status=ready, message="识别服务加载中（模型尚未加载完成）", **common)
        if _failed(start):
            # 最近一次启动作业失败且服务当前仍未响应：显示失败原因；服务之后起来时上面分支优先，不被旧失败压住
            return AsrSetupStatus(status=ready, message=f"识别服务启动失败：{start.message}",
                                  action=SetupTaskKind.asr_start, task_id=start.task_id, **common)
        detail = f"（{sidecar.error}）" if sidecar.error else ""
        return AsrSetupStatus(status=ready, message=f"识别服务未运行{detail}", action=SetupTaskKind.asr_start, **common)

    def _ollama_status(self) -> OllamaSetupStatus:
        probe = self._probes.ollama()
        install = self.tasks.latest(SetupTaskKind.ollama_install)
        start = self.tasks.latest(SetupTaskKind.ollama_start)
        pull = self.tasks.latest(SetupTaskKind.ollama_pull)
        base = dict(installed=probe.installed, version=probe.version, running=probe.running)
        if not probe.running:
            if _running(install) or _running(start):
                task = install if _running(install) else start
                return OllamaSetupStatus(model_status=SetupComponentState.missing, progress=task.progress,
                                         message=task.message, task_id=task.task_id, **base)
            if not probe.installed:
                failed = install if _failed(install) else None
                return OllamaSetupStatus(model_status=SetupComponentState.missing,
                                         message=failed.message if failed else "未安装用户级 Ollama",
                                         action=SetupTaskKind.ollama_install,
                                         task_id=failed.task_id if failed else None, **base)
            failed = start if _failed(start) else None
            return OllamaSetupStatus(model_status=SetupComponentState.missing,
                                     message=failed.message if failed else (probe.error or "Ollama 未运行"),
                                     action=SetupTaskKind.ollama_start,
                                     task_id=failed.task_id if failed else None, **base)
        if probe.has_model:
            return OllamaSetupStatus(model_status=SetupComponentState.ready, size_label=size_label(probe.model_size),
                                     message="纠错/翻译模型已就绪", **base)
        if _running(pull):
            return OllamaSetupStatus(model_status=SetupComponentState.downloading, progress=pull.progress,
                                     message=pull.message, task_id=pull.task_id, **base)
        if _failed(pull):
            return OllamaSetupStatus(model_status=SetupComponentState.failed, message=pull.message,
                                     action=SetupTaskKind.ollama_pull, task_id=pull.task_id, **base)
        return OllamaSetupStatus(model_status=SetupComponentState.missing, message="纠错/翻译模型未下载",
                                 action=SetupTaskKind.ollama_pull, **base)
