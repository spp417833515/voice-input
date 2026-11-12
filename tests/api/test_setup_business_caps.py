"""业务层 B1–B4：快捷键冲突 / 桌面守护进程能力项、ASR 状态文案、识别服务启动前显存预检。全部假探测，不调 live systemctl/gsettings/nvidia-smi。"""
from __future__ import annotations

import pytest

from _contracts import ErrorCode, LogicError, SetupTaskKind
from _linux.conflicts import ConflictScan
from _v3.production import ProductionEndpoints, asr_start_with_preflight
from _v3.setup_actions import UnitState
from _v3.setup_probe import AsrWeightsProbe, SidecarProbe, desktop_daemon_capability, hotkey_conflicts_capability
from _v3.setup_service import SetupService
from _v3.setup_tasks import SetupTaskManager
from tests.api.setup_fixtures import FakeWorld

E = ProductionEndpoints()
GATES = ("P3/P4 fixture", "真机 fixture")


def unit(name, active="inactive", sub="dead", result="success", dropins=(), load="loaded", restarts=0):
    return UnitState(unit=name, load_state=load, active_state=active, sub_state=sub, result=result, condition_result="yes",
                     condition_timestamp_mono=1, exec_main_code=0, exec_main_status=0, n_restarts=restarts,
                     exec_condition=(), exec_start=(), drop_in_paths=tuple(dropins))


def dropin(u, name="acceptance.conf"):
    return f"/home/u/.config/systemd/user/{u}.d/{name}"


def daemon_cap(states: dict, gates=GATES):
    def read(name):
        st = states.get(name)
        if isinstance(st, Exception):
            raise st
        return st or unit(name)

    return desktop_daemon_capability(read_state=read, daemon_unit=E.daemon_unit, units=E.units, unmet_gates=lambda: gates)


# ---------------------------------------------------------------- hotkey_conflicts
def test_hotkey_conflicts_none_read_is_available_with_limits_stated():
    cap = hotkey_conflicts_capability(lambda: ConflictScan((), ()))
    assert cap.id == "hotkey_conflicts" and cap.available and not cap.required
    assert "evdev" in cap.detail


def test_hotkey_conflicts_reported_and_unread_never_means_none():
    c = "screenshot alt+x 与 应用 com.pylogmon.Manggo 的全局快捷键 screenshot-recognition 的 <Alt>x 冲突"
    cap = hotkey_conflicts_capability(lambda: ConflictScan((c,), ()))
    assert not cap.available and "com.pylogmon.Manggo" in cap.detail
    cap = hotkey_conflicts_capability(lambda: ConflictScan((), ("org.gnome.mutter.keybindings：退出码 1",)))
    assert not cap.available and "未能读取" in cap.detail and "未发现冲突" not in cap.detail
    cap = hotkey_conflicts_capability(lambda: ConflictScan((c,), ("应用 x：超时",)))
    assert not cap.available and "Manggo" in cap.detail and "未能读取" in cap.detail


def test_hotkey_conflicts_scan_exception_is_visible():
    def boom():
        raise LogicError(ErrorCode.config_io, "配置文件不是合法 JSON")

    cap = hotkey_conflicts_capability(boom)
    assert not cap.available and "无法检查" in cap.detail and "合法 JSON" in cap.detail


# ---------------------------------------------------------------- desktop_daemon
def test_daemon_running_says_restart_sh():
    cap = daemon_cap({E.daemon_unit: unit(E.daemon_unit, "active", "running", dropins=[dropin(E.daemon_unit)])})
    assert cap.id == "desktop_daemon" and cap.available and "./restart.sh" in cap.detail
    assert "--acceptance" not in cap.detail


def test_partial_acceptance_like_current_machine_gives_controlled_full_command():
    # 现场 06:3x EST：server/ollama 有 acceptance.conf 且运行；daemon 只有用户 override.conf、未运行
    states = {
        E.server_unit: unit(E.server_unit, "active", "running", dropins=[dropin(E.server_unit)]),
        E.ollama_unit: unit(E.ollama_unit, "active", "running", dropins=[dropin(E.ollama_unit)]),
        E.daemon_unit: unit(E.daemon_unit, dropins=[dropin(E.daemon_unit, "override.conf")]),
    }
    cap = daemon_cap(states)
    assert not cap.available
    d = cap.detail
    assert "守护进程未运行" in d and "部分" in d and E.server_unit in d and E.ollama_unit in d and E.daemon_unit not in d
    assert "./start.sh --acceptance" in d and "不要运行无参数 ./start.sh 或桌面图标" in d and "显存预检" in d


def test_no_acceptance_and_official_mode():
    cap = daemon_cap({})
    assert not cap.available and "没有任何单元处于验收运行" in cap.detail and "./start.sh --acceptance" in cap.detail
    official = daemon_cap({}, gates=())
    assert not official.available and "./start.sh 启动" in official.detail and "--acceptance" not in official.detail


def test_daemon_failed_activating_not_loaded():
    failed = daemon_cap({E.daemon_unit: unit(E.daemon_unit, "failed", "failed", result="exit-code")})
    assert not failed.available and "启动失败" in failed.detail and "journalctl" in failed.detail
    restarting = daemon_cap({E.daemon_unit: unit(E.daemon_unit, "activating", "auto-restart", result="exit-code", restarts=2)})
    assert not restarting.available and "启动或重启中" in restarting.detail
    missing = daemon_cap({E.daemon_unit: unit(E.daemon_unit, load="not-found")})
    assert not missing.available and "未安装" in missing.detail


def test_daemon_read_failure_is_unknown():
    cap = daemon_cap({E.server_unit: LogicError(ErrorCode.dependency_failed, "无法读取 x 状态：Failed to connect to bus")})
    assert not cap.available and "无法读取守护进程状态" in cap.detail and "无法判断" in cap.detail
    assert "./restart.sh" not in cap.detail and "--acceptance" not in cap.detail


# ---------------------------------------------------------------- asr status (B2)
class FixedTasks(SetupTaskManager):
    def __init__(self, tasks):
        super().__init__({})
        self._fixed = tasks

    def latest(self, kind):
        return self._fixed.get(kind)


def task(state, message="", kind=SetupTaskKind.asr_start):
    from _contracts import SetupTask

    return SetupTask(task_id="t1", kind=kind, state=state, message=message, started_at="2026-11-11T12:00:00+00:00")


def asr(sidecar, start=None):
    world = FakeWorld(weights=AsrWeightsProbe(True, 4_400_000_000, ()), sidecar=sidecar)
    svc = SetupService(probes=world.probes(), tasks=FixedTasks({SetupTaskKind.asr_start: start}), unmet_gates=lambda: [])
    return svc.check().asr


@pytest.mark.parametrize(
    ("sidecar", "start", "running", "message", "action"),
    [
        (SidecarProbe(True, True, None), None, True, "识别服务已加载", None),
        (SidecarProbe(True, False, None), None, False, "识别服务加载中", None),
        (SidecarProbe(False, False, "识别服务未响应: refused"), None, False, "识别服务未运行（识别服务未响应: refused）", "asr_start"),
        (SidecarProbe(False, False, "识别服务未响应: refused"), task("failed", "显存不足：需要 13043 MiB"), False,
         "识别服务启动失败：显存不足：需要 13043 MiB", "asr_start"),
        # 旧失败不压住后来的事实：服务已起来（加载中/已加载）时按实时探测显示
        (SidecarProbe(True, False, None), task("failed", "旧失败"), False, "识别服务加载中", None),
        (SidecarProbe(True, True, None), task("failed", "旧失败"), True, "识别服务已加载", None),
        (SidecarProbe(False, False, "x"), task("running", "显存预检"), False, "显存预检", None),
    ],
)
def test_asr_status_weights_only_and_running_from_probe(sidecar, start, running, message, action):
    status = asr(sidecar, start)
    assert status.status.value == "ready" and status.weights_present is True
    assert status.sidecar_running is running
    assert status.message.startswith(message) and "已启动但模型未加载" not in status.message
    assert (status.action.value if status.action else None) == action


# ---------------------------------------------------------------- asr_start + preflight (B4)
class StartSpy:
    def __init__(self):
        self.calls = 0

    def __call__(self, report):
        self.calls += 1
        return "started"


def runner(active="inactive", smi="0, GPU-x, 16303, 14574, Fixture GPU\n", show_rc=0):
    calls: list[list[str]] = []

    def run(cmd, **kw):
        import subprocess

        calls.append(list(cmd))
        if cmd[0] == "systemctl":
            out = (
                f"LoadState=loaded\nActiveState={active}\nSubState=x\nResult=success\nConditionResult=yes\n"
                "ConditionTimestampMonotonic=1\nExecMainCode=0\nExecMainStatus=0\nNRestarts=0\n"
                "ExecStart={ path=/v/bin/python ; argv[]=/v/bin/python -m runtimes.asr serve --load --model /m ; pid=0 ; code=(null) ; status=0/0 }\n"
            )
            return subprocess.CompletedProcess(cmd, show_rc, out if show_rc == 0 else "", "bus down" if show_rc else "")
        return subprocess.CompletedProcess(cmd, 0, smi, "")

    run.calls = calls
    return run


def action(run, sidecar=SidecarProbe(False, False, "x"), which=lambda n: f"/usr/bin/{n}"):
    spy = StartSpy()
    act = asr_start_with_preflight(unit=E.asr_unit, start=spy, sidecar_probe=lambda: sidecar, run=run, which=which)
    return act, spy


def test_loaded_sidecar_needs_no_start_or_preflight():
    run = runner()
    act, spy = action(run, sidecar=SidecarProbe(True, True, None))
    assert "已加载" in act(lambda p: None) and spy.calls == 0 and run.calls == []


@pytest.mark.parametrize("state", ["active", "activating"])
def test_running_unit_skips_preflight_and_lets_start_judge(state):
    run = runner(active=state)
    act, spy = action(run)
    assert act(lambda p: None) == "started" and spy.calls == 1
    assert not any(c[0] == "nvidia-smi" for c in run.calls)


def test_enough_memory_then_start():
    run = runner()
    stages = []
    act, spy = action(run)
    assert act(lambda p: stages.append(p.stage)) == "started" and spy.calls == 1
    assert stages[0] == "显存预检" and "需要 13043 MiB" in stages[1]


@pytest.mark.parametrize(
    ("smi", "which", "kind", "needle"),
    [
        ("0, GPU-x, 16303, 1519, Fixture GPU\n", lambda n: f"/usr/bin/{n}", "insufficient", "显存不足"),
        ("", lambda n: None, "unknown", "未找到 nvidia-smi"),
        ("0, a, 16303, 14574, G\n1, b, 24564, 24000, H\n", lambda n: f"/usr/bin/{n}", "unknown", "2 张 GPU"),
    ],
)
def test_preflight_refusal_never_starts(smi, which, kind, needle):
    act, spy = action(runner(smi=smi), which=which)
    with pytest.raises(LogicError) as info:
        act(lambda p: None)
    assert spy.calls == 0 and info.value.code == ErrorCode.dependency_failed
    assert info.value.message.startswith("未启动识别服务：") and needle in info.value.message
    assert info.value.details["preflight"] == kind


def test_unit_state_read_failure_is_not_treated_as_active():
    act, spy = action(runner(show_rc=1))
    with pytest.raises(LogicError) as info:
        act(lambda p: None)
    assert spy.calls == 0 and "无法读取" in info.value.message


def test_preflight_cli_used_by_start_sh_prints_only_the_reason(tmp_path):
    # start.sh 把 CLI 输出原样显示给用户；PATH 置空保证碰不到 live systemctl/nvidia-smi → unknown
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run([sys.executable, "-m", "_v3.gpu_preflight", "asr", "--unit", E.asr_unit], cwd=root,
                          env={"PATH": str(tmp_path), "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "RuntimeWarning" not in proc.stderr and proc.stdout == ""
    assert proc.stderr.strip() and "\n" not in proc.stderr.strip()
