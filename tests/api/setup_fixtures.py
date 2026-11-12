"""设置页夹具：显式假探测与可控假动作。不触网、不调 systemctl、不读 live 文件。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from _contracts import ErrorCode, LogicError, SetupCapability, SetupProgress, SetupTaskKind
from _v3.setup_probe import AsrWeightsProbe, OllamaProbe, SidecarProbe, probe_capabilities
from _v3.setup_service import SetupProbes, SetupService
from _v3.setup_tasks import SetupTaskManager

FAKE_LAN_IPS = ["192.168.10.20", "10.1.2.3"]


@dataclass
class FakeWorld:
    session: str = "wayland"
    ollama: OllamaProbe = field(default_factory=lambda: OllamaProbe(True, True, "0.40.16", False, None, None))
    weights: AsrWeightsProbe = field(default_factory=lambda: AsrWeightsProbe(False, 0, ("config.json",)))
    sidecar: SidecarProbe = field(default_factory=lambda: SidecarProbe(False, False, "识别服务未响应: fixture"))
    tools: dict[str, str] = field(default_factory=lambda: {"xclip": "/usr/bin/xclip"})
    portals: dict[str, int] = field(default_factory=lambda: {
        "org.freedesktop.portal.Screenshot": 2,
        "org.freedesktop.portal.GlobalShortcuts": 1,
    })
    langs: list[str] = field(default_factory=lambda: ["eng", "chi_sim", "osd"])

    def capabilities(self, session: str) -> list[SetupCapability]:
        return probe_capabilities(
            session=session,
            which=self.tools.get,
            portal_version=lambda iface: (self.portals.get(iface), None if iface in self.portals else "fixture: 无此接口"),
            ocr_languages=lambda: list(self.langs),
        )

    def probes(self) -> SetupProbes:
        return SetupProbes(
            session=lambda: self.session,
            ollama=lambda: self.ollama,
            asr_weights=lambda: self.weights,
            asr_sidecar=lambda: self.sidecar,
            capabilities=self.capabilities,
        )


class GatedAction:
    """进度可观察、由测试放行的假动作。fail=True 时以 LogicError 结束。"""

    def __init__(self, *, fail: str | None = None, on_success=None) -> None:
        self.release = threading.Event()
        self.reported = threading.Event()
        self.fail = fail
        self.on_success = on_success
        self.calls = 0

    def __call__(self, report) -> str:
        self.calls += 1
        report(SetupProgress(completed_bytes=50, total_bytes=200, percent=25.0, stage="fixture 下载中"))
        self.reported.set()
        assert self.release.wait(5), "测试未放行假动作"
        if self.fail:
            raise LogicError(ErrorCode.dependency_failed, self.fail)
        if self.on_success:
            self.on_success()
        return "fixture 完成"


def build_setup(world: FakeWorld, actions: dict[SetupTaskKind, GatedAction] | None = None, gates=("fixture gate",)):
    return SetupService(
        probes=world.probes(),
        tasks=SetupTaskManager(actions or {}),
        unmet_gates=lambda: list(gates),
    )
