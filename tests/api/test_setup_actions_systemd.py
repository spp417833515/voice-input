"""L3：systemd_user_start_action 按真实单元状态判定（假 systemctl，不调 live systemctl）。

本机证据（06:3x EST 只读 systemctl --user show）：asr 被门闩跳过时 Result=exec-condition、ConditionResult=no、
ExecCondition `… _activation.py check` status=78；从未启动的 daemon ConditionResult=no、Result=success、
ConditionTimestampMonotonic=0。
"""
from __future__ import annotations

import subprocess

import pytest

from _contracts import ErrorCode, LogicError
from _v3.setup_actions import systemd_unit_state, systemd_user_start_action

UNIT = "ollama-voice-asr.service"
GATE = (
    "{ path=/p/.venv/bin/python3 ; argv[]=/p/.venv/bin/python3 /p/_activation.py check ; ignore_errors=no ; "
    "start_time=[n/a] ; stop_time=[n/a] ; pid=11 ; code=exited ; status=%s }"
)
UNIT_CAT = (
    "# /home/u/.config/systemd/user/ollama-voice-asr.service\n[Unit]\nDescription=x\n"
    "ConditionPathExists=/m/Qwen3-ASR-1.7B/config.json\nConditionPathExists=/v/bin/python\n[Service]\nExecStart=/v/bin/python -m x\n"
)


def show(active="inactive", sub="dead", result="success", cond="yes", ts=100, gate_status="0/0", restarts=0, load="loaded"):
    return (
        f"LoadState={load}\nActiveState={active}\nSubState={sub}\nResult={result}\nConditionResult={cond}\n"
        f"ConditionTimestampMonotonic={ts}\nExecMainCode=0\nExecMainStatus=0\nNRestarts={restarts}\n"
        f"ExecCondition={GATE % gate_status}\n"
        "ExecStart={ path=/v/bin/python ; argv[]=/v/bin/python -m runtimes.asr serve --load ; ignore_errors=no ; pid=0 ; code=(null) ; status=0/0 }\n"
    )


class FakeSystemctl:
    """show 依次返回 shows 中的输出（最后一个重复）；start 返回 start_rc；cat 返回单元文本。"""

    def __init__(self, shows, *, start_rc=0, show_rc=0, cat=UNIT_CAT):
        self.shows = list(shows)
        self.start_rc = start_rc
        self.show_rc = show_rc
        self.cat = cat
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        verb = cmd[2]
        if verb == "show":
            out = self.shows.pop(0) if len(self.shows) > 1 else self.shows[0]
            return subprocess.CompletedProcess(cmd, self.show_rc, out if self.show_rc == 0 else "", "Failed to connect to bus" if self.show_rc else "")
        if verb == "start":
            return subprocess.CompletedProcess(cmd, self.start_rc, "", "Unit u.service not found." if self.start_rc else "")
        if verb == "cat":
            return subprocess.CompletedProcess(cmd, 0, self.cat, "")
        raise AssertionError(cmd)

    def verbs(self):
        return [c[2] for c in self.calls]


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps += 1
        self.t += s


def run_action(fake, *, ready=lambda: False, timeout_s=300.0, exists=lambda p: True):
    clock = Clock()
    action = systemd_user_start_action(unit=UNIT, ready=ready, run=fake, timeout_s=timeout_s, sleep=clock.sleep, clock=clock, exists=exists)
    return action, clock


def fail(fake, **kw):
    action, clock = run_action(fake, **kw)
    with pytest.raises(LogicError) as info:
        action(lambda _p: None)
    return info.value, clock


def test_gate_rejection_fails_immediately_without_waiting():
    fake = FakeSystemctl([show(ts=100), show(result="exec-condition", cond="no", ts=200, gate_status="78")])
    err, clock = fail(fake)
    assert err.code == ErrorCode.dependency_failed and err.details["verdict"] == "gated"
    assert "./start.sh --acceptance" in err.message and "78" in err.message
    assert clock.sleeps == 0 and fake.verbs() == ["show", "start", "show"]


def test_exec_condition_not_from_project_gate_is_not_called_gated():
    fake = FakeSystemctl([show(ts=100), show(result="exec-condition", cond="no", ts=200, gate_status="1")])
    err, clock = fail(fake)
    assert err.details["verdict"] == "exec_condition" and "无法确认是本项目门闩" in err.message
    assert clock.sleeps == 0


def test_condition_path_failure_names_the_real_missing_file():
    fake = FakeSystemctl([show(ts=100), show(cond="no", ts=200)])
    err, clock = fail(fake, exists=lambda p: not p.endswith("config.json"))
    assert err.details["verdict"] == "condition"
    assert "缺失：/m/Qwen3-ASR-1.7B/config.json" in err.message and "/v/bin/python" not in err.message
    assert clock.sleeps == 0


def test_condition_failure_without_identified_path_is_generic():
    fake = FakeSystemctl([show(ts=100), show(cond="no", ts=200)])
    err, _ = fail(fake, exists=lambda p: True)
    assert err.details["verdict"] == "condition"
    assert "未定位到缺失" in err.message and "下载" not in err.message


def test_drop_in_reset_and_negation_in_conditions():
    cat = UNIT_CAT + "# drop-in\n[Unit]\nConditionPathExists=\nConditionPathExists=!/tmp/block\n"
    fake = FakeSystemctl([show(ts=100), show(cond="no", ts=200)], cat=cat)
    err, _ = fail(fake, exists=lambda p: True)
    assert "存在但要求不存在：/tmp/block" in err.message and "config.json" not in err.message


def test_failed_unit_fails_immediately():
    fake = FakeSystemctl([show(ts=100), show(active="failed", sub="failed", result="exit-code", ts=200)])
    err, clock = fail(fake)
    assert err.details["verdict"] == "failed" and "Result=exit-code" in err.message and "journalctl" in err.message
    assert clock.sleeps == 0


def test_show_read_failure_fails_without_start_or_wait():
    fake = FakeSystemctl([show()], show_rc=1)
    err, clock = fail(fake)
    assert "无法读取" in err.message and "Failed to connect to bus" in err.message
    assert fake.verbs() == ["show"] and clock.sleeps == 0


def test_show_missing_fields_is_read_failure():
    fake = FakeSystemctl(["ActiveState=active\n"])
    err, _ = fail(fake)
    assert "缺少" in err.message


def test_stale_state_with_unchanged_condition_timestamp_is_not_running():
    stale = show(result="exec-condition", cond="no", ts=100, gate_status="78")
    fake = FakeSystemctl([stale, stale])
    err, clock = fail(fake)
    assert err.details["verdict"] == "not_running" and clock.sleeps == 0


def test_activating_then_active_then_ready_succeeds():
    fake = FakeSystemctl([show(ts=100), show(active="activating", sub="start", ts=200), show(active="active", sub="running", ts=200)])
    states = iter([False, False, True])
    action, clock = run_action(fake, ready=lambda: next(states))
    assert "已就绪" in action(lambda _p: None)
    assert fake.verbs().count("start") == 1 and clock.sleeps == 2


def test_crash_during_polling_is_detected_promptly():
    running = show(active="active", sub="running", ts=200)
    crashed = show(active="activating", sub="auto-restart", result="exit-code", ts=200, restarts=1)
    fake = FakeSystemctl([show(ts=100), running, running, running, crashed])
    err, clock = fail(fake, timeout_s=300)
    assert err.details["verdict"] == "failed" and "auto-restart" in err.message
    assert clock.sleeps == 2 and fake.verbs().count("start") == 1


def test_exit_during_polling_is_detected():
    fake = FakeSystemctl([show(ts=100), show(active="active", sub="running", ts=200), show(active="inactive", sub="dead", ts=200)])
    err, clock = fail(fake)
    assert "已不在运行" in err.message and clock.sleeps == 0


def test_systemctl_start_nonzero_and_timeout_semantics_kept():
    err, _ = fail(FakeSystemctl([show()], start_rc=5))
    assert "not found" in err.message
    fake = FakeSystemctl([show(ts=100), show(active="active", sub="running", ts=200)])
    err, clock = fail(fake, timeout_s=3)
    assert err.code == ErrorCode.timeout and clock.sleeps == 3


def test_systemd_unit_state_parses_exec_commands():
    fake = FakeSystemctl([show(result="exec-condition", cond="no", gate_status="78")])
    state = systemd_unit_state(UNIT, run=fake)
    assert state.exec_condition[0].argv[-2:] == ("/p/_activation.py", "check") and state.exec_condition[0].status == 78
    assert state.exec_start[0].argv == ("/v/bin/python", "-m", "runtimes.asr", "serve", "--load")
    assert fake.calls[0][:4] == ["systemctl", "--user", "show", UNIT]
