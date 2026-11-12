"""假子进程（含孙进程）验证 sidecar 退出回收。不启 GPU、不绑 17946。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from runtimes.asr.process_guard import ChildProcessGuard, descendants_of, is_alive

ROOT = Path("/home/spp/桌面/ollama-voice-input")
TMP = ROOT / ".pp" / "tmp" / "sidecar" / "process-guard"
HOST = r'''
import os, signal, sys, time, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from runtimes.asr.process_guard import ChildProcessGuard

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
mode = sys.argv[3]
ready = tmp / "host-ready.txt"
guard = ChildProcessGuard(term_timeout_s=2.0, kill_timeout_s=1.0)
guard.enter_new_session()
child_script = tmp / "child_tree.py"
child = subprocess.Popen([sys.executable, str(child_script), str(tmp / "tree-pids.txt")], start_new_session=False)
# wait until grandchild is recorded
for _ in range(50):
    if (tmp / "tree-pids.txt").exists():
        break
    time.sleep(0.05)
guard.remember()
(tmp / "host-pids.txt").write_text(
    f"host={os.getpid()} pgid={os.getpgrp()} child={child.pid} owned={sorted(guard.owned)}\n",
    encoding="utf-8",
)
ready.write_text(str(os.getpid()), encoding="utf-8")
if mode == "term-wait":
    def _stop(signum):
        guard.remember()
        guard.reap()
        raise SystemExit(0)
    guard.install(request_stop=_stop)
    time.sleep(30)
    guard.remember()
    guard.reap()
elif mode == "sigint-wait":
    def _stop(signum):
        guard.remember()
        guard.reap()
        raise SystemExit(0)
    guard.install(request_stop=_stop)
    time.sleep(30)
    guard.remember()
    guard.reap()
elif mode == "fail":
    try:
        raise RuntimeError("startup failed after spawning children")
    finally:
        guard.remember()
        guard.reap()
else:
    raise SystemExit(f"unknown mode {mode}")
'''

CHILD = r'''
import subprocess, sys, time, os
from pathlib import Path
out = Path(sys.argv[1])
grand = subprocess.Popen([sys.executable, "-c", "import time,sys; sys.argv[0]='sidecar-fake-grandchild'; time.sleep(60)"])
out.write_text(f"{os.getpid()} {grand.pid}\n", encoding="utf-8")
time.sleep(60)
'''


def _write_helpers() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    (TMP / "child_tree.py").write_text(CHILD, encoding="utf-8")
    (TMP / "host_tree.py").write_text(HOST, encoding="utf-8")


def _start_bystander() -> subprocess.Popen:
    # 独立会话、同风格名字，证明不按名字杀
    return subprocess.Popen(
        [sys.executable, "-c", "import time,sys; sys.argv[0]='sidecar-fake-grandchild'; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_ready(path: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return int(path.read_text(encoding="utf-8").strip())
        time.sleep(0.05)
    raise AssertionError(f"host not ready: {path}")


def _tree_pids() -> tuple[int, int]:
    text = (TMP / "tree-pids.txt").read_text(encoding="utf-8").strip()
    child_s, grand_s = text.split()
    return int(child_s), int(grand_s)


def _run_host(mode: str) -> subprocess.Popen:
    _write_helpers()
    for name in ("host-ready.txt", "tree-pids.txt", "host-pids.txt"):
        path = TMP / name
        if path.exists():
            path.unlink()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.Popen(
        [sys.executable, str(TMP / "host_tree.py"), str(ROOT), str(TMP), mode],
        cwd=str(ROOT),
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _cleanup(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    proc.wait(timeout=3)


@pytest.fixture
def bystander():
    proc = _start_bystander()
    try:
        time.sleep(0.1)
        assert is_alive(proc.pid)
        yield proc
    finally:
        _cleanup(proc)


def test_descendants_table_includes_grandchild():
    table = {
        1: (0, 1, 1),
        10: (1, 10, 10),
        11: (10, 10, 10),
        12: (11, 10, 10),
        99: (1, 99, 99),
    }
    assert descendants_of(10, table) == {11, 12}
    assert 99 not in descendants_of(10, table)


def test_sigterm_reaps_grandchild_not_bystander(bystander):
    host = _run_host("term-wait")
    try:
        host_pid = _wait_ready(TMP / "host-ready.txt")
        child_pid, grand_pid = _tree_pids()
        assert is_alive(child_pid)
        assert is_alive(grand_pid)
        assert is_alive(bystander.pid)
        os.kill(host_pid, signal.SIGTERM)
        host.wait(timeout=8)
        time.sleep(0.2)
        assert not is_alive(child_pid)
        assert not is_alive(grand_pid)
        assert is_alive(bystander.pid)
        (TMP / "term-evidence.txt").write_text(
            f"host={host_pid} child={child_pid} grand={grand_pid} bystander={bystander.pid} host_exit={host.returncode}\n",
            encoding="utf-8",
        )
    finally:
        _cleanup(host)


def test_sigint_reaps_grandchild_not_bystander(bystander):
    host = _run_host("sigint-wait")
    try:
        host_pid = _wait_ready(TMP / "host-ready.txt")
        child_pid, grand_pid = _tree_pids()
        os.kill(host_pid, signal.SIGINT)
        host.wait(timeout=8)
        time.sleep(0.2)
        assert not is_alive(child_pid)
        assert not is_alive(grand_pid)
        assert is_alive(bystander.pid)
    finally:
        _cleanup(host)


def test_startup_failure_reaps_children(bystander):
    host = _run_host("fail")
    try:
        # fail mode reaps in finally and exits; wait for it
        host.wait(timeout=8)
        # tree pids should have been written before the raise
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (TMP / "tree-pids.txt").exists():
            time.sleep(0.05)
        assert (TMP / "tree-pids.txt").exists()
        child_pid, grand_pid = _tree_pids()
        time.sleep(0.2)
        assert not is_alive(child_pid)
        assert not is_alive(grand_pid)
        assert is_alive(bystander.pid)
        assert host.returncode != 0
    finally:
        _cleanup(host)


def test_zombie_child_is_not_alive():
    """已退出但父进程尚未 wait 的子进程（僵尸）不得被报告为未回收。"""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{child.pid}/stat").read_text()
            if stat[stat.rfind(")") + 2] == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("子进程未在时限内变成僵尸")
        assert is_alive(child.pid) is False
    finally:
        child.wait(timeout=5)
    assert is_alive(child.pid) is False


def test_running_child_is_alive():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert is_alive(child.pid) is True
    finally:
        child.kill()
        child.wait(timeout=5)
