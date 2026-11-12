"""启动入口 fail-closed（审查 B1/B2/B3）：命令桩确定性证明。

临时项目副本 + 假 HOME + PATH 里的 systemctl/sudo/apt/usermod/udevadm/pip/sleep 桩，只记录调用；
从不调用真实 systemctl、不 sudo、不 pip、不启动任何服务、不碰用户 ~/.config/systemd。
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _activation
from _activation import ACCEPTANCE_ENV, EXIT_GATED, activation_mode
from _v3.production import CODE_ROOT
from _v3.units import UNIT_NAMES, render_all

ROOT = CODE_ROOT
UNITS = list(UNIT_NAMES)

SYSTEMCTL_STUB = r"""#!/bin/bash
echo "systemctl $*" >> "$STUB_LOG"
[ "$1" = "--user" ] && shift
cmd="$1"; shift || true
case "$cmd" in
  show-environment|daemon-reload|import-environment|enable|disable) exit 0 ;;
  is-active)
    [ "$1" = "--quiet" ] && shift
    [ -f "$STUB_STATE/active/$1" ] && exit 0 || exit 3 ;;
  start|restart)
    for u in "$@"; do touch "$STUB_STATE/active/$u"; done; exit 0 ;;
  stop)
    for u in "$@"; do rm -f "$STUB_STATE/active/$u"; done; exit 0 ;;
  show) echo yes; exit 0 ;;
esac
exit 0
"""

LOGGING_STUB = """#!/bin/bash
echo "$(basename "$0") $*" >> "$STUB_LOG"
exit 0
"""


class Sandbox:
    def __init__(self, tmp: Path, *, official: bool = False) -> None:
        self.tmp = tmp
        self.proj = tmp / "proj"
        self.home = tmp / "home"
        self.state = tmp / "state"
        self.log = tmp / "calls.log"
        self.bin = tmp / "bin"
        for d in (self.proj, self.home, self.state / "active", self.bin, self.home / "Desktop"):
            d.mkdir(parents=True, exist_ok=True)
        for name in ("start.sh", "restart.sh", "install.sh", "requirements.txt"):
            shutil.copy2(ROOT / name, self.proj / name)
        gate_src = (ROOT / "_activation.py").read_text(encoding="utf-8")
        if official:
            # 仅测试夹具：临时副本里模拟“官方门闩已全部关闭”的未来状态；产品源码常量不动。
            start = gate_src.index("PRODUCTION_UNMET_GATES: tuple[str, ...] = (")
            end = gate_src.index(")\n", start) + 2
            gate_src = gate_src[:start] + "PRODUCTION_UNMET_GATES: tuple[str, ...] = ()\n" + gate_src[end:]
        (self.proj / "_activation.py").write_text(gate_src, encoding="utf-8")
        venv_bin = self.proj / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        py = venv_bin / "python3"
        # 显存预检桩：`-m _v3.gpu_preflight` 只记录并按 STUB_PREFLIGHT_RC 退出（默认 0=够），不跑 nvidia-smi / systemctl。
        py.write_text(
            "#!/bin/bash\n"
            'if [ "$1" = "-m" ] && [ "$2" = "_v3.gpu_preflight" ]; then\n'
            '  echo "preflight $*" >> "$STUB_LOG"\n'
            '  rc="${STUB_PREFLIGHT_RC:-0}"\n'
            '  if [ "$rc" = 0 ]; then echo "显存预检通过：stub"; else echo "显存不足：stub 需要 13043 MiB 空闲 1519 MiB" >&2; fi\n'
            '  exit "$rc"\n'
            "fi\n"
            f'PYTHONPATH="{ROOT}" exec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        (venv_bin / "pip").write_text(LOGGING_STUB, encoding="utf-8")
        (self.bin / "systemctl").write_text(SYSTEMCTL_STUB, encoding="utf-8")
        for name in ("sudo", "apt", "dnf", "pacman", "usermod", "udevadm", "sleep", "fuser"):
            (self.bin / name).write_text(LOGGING_STUB, encoding="utf-8")
        (self.bin / "xdg-user-dir").write_text(f'#!/bin/bash\necho "{self.home / "Desktop"}"\n', encoding="utf-8")
        (self.bin / "groups").write_text("#!/bin/bash\necho user input\n", encoding="utf-8")
        for p in list(self.bin.iterdir()) + [py, venv_bin / "pip"]:
            p.chmod(0o755)
        self.log.write_text("", encoding="utf-8")

    @property
    def unit_dir(self) -> Path:
        return self.home / ".config" / "systemd" / "user"

    def run(self, script: str, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "USER": "tester",
            "XDG_SESSION_TYPE": "x11",
            "LANG": "C.UTF-8",
        }
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(self.proj / script), *args], env=env, capture_output=True, text=True, timeout=120
        )

    def calls(self) -> list[str]:
        return [l for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def verbs(self) -> list[str]:
        out = []
        for c in self.calls():
            parts = c.split()
            if parts[0] == "systemctl":
                out.append(parts[2] if parts[1] == "--user" else parts[1])
        return out

    def privileged(self) -> list[str]:
        return [c for c in self.calls() if c.split()[0] in {"sudo", "apt", "dnf", "pacman", "usermod", "udevadm"}]

    def activate(self, *units: str) -> None:
        for u in units:
            (self.state / "active" / u).touch()

    def active(self) -> set[str]:
        return {p.name for p in (self.state / "active").iterdir()}

    def dropins(self) -> list[Path]:
        return sorted(self.unit_dir.glob("*.service.d/acceptance.conf"))


# ---------------------------------------------------------------- 门闩模块
def test_activation_single_source_and_modes():
    from _v3 import runtime

    assert runtime.PRODUCTION_UNMET_GATES is _activation.PRODUCTION_UNMET_GATES
    assert _activation.PRODUCTION_UNMET_GATES, "官方门闩未关闭前不得清空"
    # P2 已按 r9 证据关闭；剩余恰为 4b（P3/P4）与真机两项，不得多删
    gates = _activation.PRODUCTION_UNMET_GATES
    assert len(gates) == 2
    assert not any("P2" in g or "1.515" in g for g in gates)
    assert any("qwen3.5:4b" in g and "P3/P4" in g for g in gates)
    assert any("真机" in g and "WebKit" in g for g in gates)
    assert activation_mode({}) == "blocked"
    assert activation_mode({ACCEPTANCE_ENV: "1"}) == "acceptance"
    assert activation_mode({ACCEPTANCE_ENV: "true"}) == "blocked"


def test_activation_cli_exit_codes():
    script = ROOT / "_activation.py"
    env = {k: v for k, v in os.environ.items() if k != ACCEPTANCE_ENV}
    blocked = subprocess.run(["python3", str(script), "check"], env=env, capture_output=True, text=True)
    assert blocked.returncode == EXIT_GATED and "P3/P4" in blocked.stderr and "真机" in blocked.stderr
    ok = subprocess.run(
        ["python3", str(script), "check"], env={**env, ACCEPTANCE_ENV: "1"}, capture_output=True, text=True
    )
    assert ok.returncode == 0 and ok.stdout.strip() == "acceptance"


def test_app_factory_exits_78_before_touching_live_data(monkeypatch):
    import app

    monkeypatch.delenv(ACCEPTANCE_ENV, raising=False)

    def forbidden(**_kw):
        raise AssertionError("门闩未关时不得装配 live 应用")

    monkeypatch.setattr("_v3.production.build_production_app", forbidden)
    with pytest.raises(SystemExit) as exc:
        app.create_production_app()
    assert exc.value.code == EXIT_GATED


def test_daemon_main_checks_gate_before_any_device():
    tree = ast.parse((ROOT / "daemon.py").read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    first = main.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    assert getattr(first.value.func, "id", None) == "exit_if_blocked"


def test_every_unit_has_gate_condition_and_no_restart_loop(tmp_path):
    units = render_all(project_dir=tmp_path / "proj", venv=Path(sys.executable).parent.parent)
    for name, text in units.items():
        cond = [l for l in text.splitlines() if l.startswith("ExecCondition=")]
        assert len(cond) == 1 and cond[0].endswith("_activation.py check"), name
        assert "RestartPreventExitStatus=78" in text, name
        assert ACCEPTANCE_ENV not in text, name  # 正式单元不带验收开关
    # 真跑渲染出来的 ExecCondition 命令（解释器换成当前测试解释器，脚本为真实 _activation.py）
    cmd = [sys.executable, str(ROOT / "_activation.py"), "check"]
    env = {k: v for k, v in os.environ.items() if k != ACCEPTANCE_ENV}
    assert subprocess.run(cmd, env=env, capture_output=True).returncode == EXIT_GATED  # 1..254 → systemd 跳过
    assert subprocess.run(cmd, env={**env, ACCEPTANCE_ENV: "1"}, capture_output=True).returncode == 0


# ---------------------------------------------------------------- B1
def test_b1_default_start_with_gates_open_is_zero_side_effect(tmp_path):
    sb = Sandbox(tmp_path)
    res = sb.run("start.sh")
    assert res.returncode == EXIT_GATED, res.stdout + res.stderr
    assert "P3/P4" in res.stderr and "真机" in res.stderr and "--acceptance" in res.stderr
    verbs = sb.verbs()
    assert not {"start", "enable", "restart"} & set(verbs), verbs
    assert sb.privileged() == []
    assert not any(c.startswith("pip ") for c in sb.calls())  # 门闩先于依赖安装
    assert not sb.unit_dir.exists() or not list(sb.unit_dir.glob("*.service"))  # 未同步/写单元
    assert sb.active() == set()


def test_b1_desktop_entry_and_inherited_env_cannot_bypass(tmp_path):
    sb = Sandbox(tmp_path)
    # 用户环境里残留 OVI_ACCEPTANCE_RUN=1 也不能让无参 start 变成验收
    res = sb.run("start.sh", extra_env={ACCEPTANCE_ENV: "1"})
    assert res.returncode == EXIT_GATED
    assert not {"start", "enable"} & set(sb.verbs())
    desktop = (ROOT / "ollama-voice-input.desktop").read_text(encoding="utf-8")
    assert "--acceptance" not in desktop


def test_b1_acceptance_starts_explicitly_without_enable_or_sudo(tmp_path):
    sb = Sandbox(tmp_path)
    res = sb.run("start.sh", "--acceptance")
    assert res.returncode == 0, res.stdout + res.stderr
    verbs = sb.verbs()
    assert "enable" not in verbs and sb.privileged() == []
    started = [c.split()[-1] for c in sb.calls() if c.startswith("systemctl --user start ")]
    assert started == UNITS_ORDER
    assert [p.parent.name for p in sb.dropins()] == sorted(f"{u}.d" for u in UNITS)
    for p in sb.dropins():
        assert p.read_text(encoding="utf-8") == "[Service]\nEnvironment=OVI_ACCEPTANCE_RUN=1\n"
    rendered = sorted(p.name for p in sb.unit_dir.glob("*.service"))
    assert rendered == sorted(UNITS)
    assert "ExecCondition=" in (sb.unit_dir / "ollama-voice-asr.service").read_text(encoding="utf-8")


UNITS_ORDER = [
    "ollama-voice-ollama.service",
    "ollama-voice-asr.service",
    "ollama-voice-server.service",
    "ollama-voice-daemon.service",
]


def test_b1_official_future_start_never_enables_install_does(tmp_path):
    sb = Sandbox(tmp_path, official=True)
    res = sb.run("start.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "enable" not in sb.verbs() and sb.privileged() == []
    assert sb.dropins() == []
    sb2 = Sandbox(tmp_path / "inst", official=True)
    res2 = sb2.run("install.sh")
    assert res2.returncode == 0, res2.stdout + res2.stderr
    enabled = [c.split()[-1] for c in sb2.calls() if c.startswith("systemctl --user enable ")]
    assert sorted(enabled) == sorted(UNITS)
    assert "start" not in sb2.verbs()


def test_b1_full_install_with_gates_open_writes_units_but_no_autostart(tmp_path):
    sb = Sandbox(tmp_path)
    res = sb.run("install.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert not {"enable", "start", "restart"} & set(sb.verbs())
    assert sorted(p.name for p in sb.unit_dir.glob("*.service")) == sorted(UNITS)
    assert "未设开机自启" in res.stdout


# ---------------------------------------------------------------- B2
def test_b2_revoking_acceptance_stops_old_processes_and_refuses(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.run("start.sh", "--acceptance").returncode == 0
    assert sb.active() == set(UNITS)
    sb.log.write_text("", encoding="utf-8")
    res = sb.run("start.sh")
    assert res.returncode == EXIT_GATED, res.stdout + res.stderr
    assert sb.dropins() == []
    assert sb.active() == set(), "验收进程不得在撤销后继续运行"
    stop = [c for c in sb.calls() if c.startswith("systemctl --user stop ")]
    assert stop and stop[0].split()[3:] == [
        "ollama-voice-daemon.service",
        "ollama-voice-server.service",
        "ollama-voice-asr.service",
        "ollama-voice-ollama.service",
    ]
    assert not {"start", "enable", "restart"} & set(sb.verbs())
    # daemon-reload 在 stop 之前：撤销的环境对后续任何拉起都已失效
    verbs = sb.verbs()
    assert verbs.index("daemon-reload") < verbs.index("stop")


def test_b2_stray_running_units_without_dropin_are_stopped_when_blocked(tmp_path):
    sb = Sandbox(tmp_path)
    sb.activate("ollama-voice-asr.service", "ollama-voice-daemon.service")
    res = sb.run("start.sh")
    assert res.returncode == EXIT_GATED
    assert sb.active() == set()


def test_b2_official_future_revocation_restarts_fresh(tmp_path):
    sb = Sandbox(tmp_path, official=True)
    assert sb.run("start.sh", "--acceptance").returncode == 0
    sb.log.write_text("", encoding="utf-8")
    assert sb.run("start.sh").returncode == 0
    verbs = sb.verbs()
    assert verbs.index("stop") < verbs.index("start")  # 先停验收进程再按正式模式启动
    assert sb.dropins() == []


def test_b2_restart_refuses_when_gated_and_not_in_acceptance(tmp_path):
    sb = Sandbox(tmp_path)
    res = sb.run("restart.sh")
    assert res.returncode == EXIT_GATED
    assert not {"restart", "start", "enable"} & set(sb.verbs())
    assert not sb.unit_dir.exists() or not list(sb.unit_dir.glob("*.service"))


def test_b2_restart_inside_acceptance_session_only_restarts(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.run("start.sh", "--acceptance").returncode == 0
    sb.log.write_text("", encoding="utf-8")
    res = sb.run("restart.sh")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "enable" not in sb.verbs() and sb.privileged() == []
    assert any(c == "systemctl --user restart ollama-voice-server.service ollama-voice-daemon.service" for c in sb.calls())
    assert len(sb.dropins()) == 4  # 同步单元不删验收 drop-in


# ---------------------------------------------------------------- B3
def test_b3_install_update_only_renders_and_reloads(tmp_path):
    sb = Sandbox(tmp_path)
    res = sb.run("install.sh", "--update")
    assert res.returncode == 0, res.stdout + res.stderr
    assert sb.calls() == ["systemctl --user daemon-reload"], sb.calls()
    assert sorted(p.name for p in sb.unit_dir.glob("*.service")) == sorted(UNITS)
    assert not (sb.home / ".local").exists()  # 无图标/桌面文件/菜单
    assert not list((sb.home / "Desktop").iterdir())


def test_b3_update_without_venv_fails_without_side_effects(tmp_path):
    sb = Sandbox(tmp_path)
    shutil.rmtree(sb.proj / ".venv")
    res = sb.run("install.sh", "--update")
    assert res.returncode != 0
    assert sb.calls() == []


def test_b3_start_and_restart_sync_only_via_update(tmp_path):
    for name in ("start.sh", "restart.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert 'bash "$DIR/install.sh" --update' in text
        assert "sudo" not in text.replace("从不 sudo", "").replace("不 sudo", "")
        assert "systemctl --user enable" not in text


# ---------------------------------------------------------------- 显存预检接线（本轮 B5）
def _started(sb: Sandbox) -> list[str]:
    return [c.split()[-1] for c in sb.calls() if c.startswith("systemctl --user start ")]


def test_preflight_runs_once_before_asr_start_with_real_unit_name(tmp_path):
    sb = Sandbox(tmp_path)
    res = sb.run("start.sh", "--acceptance")
    assert res.returncode == 0, res.stdout + res.stderr
    calls = sb.calls()
    pre = [i for i, c in enumerate(calls) if c.startswith("preflight ")]
    assert len(pre) == 1 and calls[pre[0]] == "preflight -m _v3.gpu_preflight asr --unit ollama-voice-asr.service"
    asr_start = calls.index("systemctl --user start ollama-voice-asr.service")
    assert pre[0] < asr_start
    assert "服务已就绪" in res.stdout and "显存预检通过" in res.stdout


@pytest.mark.parametrize("rc", ["1", "2"])
def test_preflight_refusal_skips_asr_but_starts_others_and_fails(tmp_path, rc):
    sb = Sandbox(tmp_path)
    res = sb.run("start.sh", "--acceptance", extra_env={"STUB_PREFLIGHT_RC": rc})
    assert res.returncode != 0, res.stdout + res.stderr
    assert _started(sb) == ["ollama-voice-ollama.service", "ollama-voice-server.service", "ollama-voice-daemon.service"]
    assert "服务已就绪" not in res.stdout
    assert "未全部就绪" in res.stdout and "Qwen3-ASR" in res.stdout and "需要 13043 MiB" in res.stdout
    assert len(sb.dropins()) == 4 and "enable" not in sb.verbs() and sb.privileged() == []


def test_preflight_skipped_when_asr_already_active(tmp_path):
    sb = Sandbox(tmp_path)
    sb.activate("ollama-voice-asr.service")
    res = sb.run("start.sh", "--acceptance", extra_env={"STUB_PREFLIGHT_RC": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert not any(c.startswith("preflight ") for c in sb.calls())
    assert "ollama-voice-asr.service" not in _started(sb)


def test_preflight_not_reached_when_gated(tmp_path):
    sb = Sandbox(tmp_path)
    res = sb.run("start.sh", extra_env={"STUB_PREFLIGHT_RC": "1"})
    assert res.returncode == EXIT_GATED
    assert not any(c.startswith("preflight ") for c in sb.calls())


def test_backend_acceptance_dropin_name_and_units_match_entry_scripts():
    from _v3.production import ProductionEndpoints
    from _v3.setup_probe import ACCEPTANCE_DROPIN

    text = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert f'ACCEPTANCE_CONF="{ACCEPTANCE_DROPIN}"' in text
    assert set(ProductionEndpoints().units) == set(UNIT_NAMES)
    assert 'ASR_SERVICE="ollama-voice-asr.service"' in text and ProductionEndpoints().asr_unit == "ollama-voice-asr.service"
