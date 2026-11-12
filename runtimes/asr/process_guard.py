"""只回收本 sidecar 进程派生的孩子。不按进程名扫杀，不向调用方进程组发信号。"""
from __future__ import annotations

import atexit
import os
import signal
import time
from pathlib import Path

_PROC = Path("/proc")


def parse_stat(pid: int) -> tuple[int, int, int] | None:
    """返回 (ppid, pgrp, session)；进程不存在时为 None。"""
    try:
        text = (_PROC / str(pid) / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    rparen = text.rfind(")")
    if rparen < 0:
        return None
    fields = text[rparen + 2 :].split()
    if len(fields) < 4:
        return None
    return int(fields[1]), int(fields[2]), int(fields[3])


def iter_process_table() -> dict[int, tuple[int, int, int]]:
    table: dict[int, tuple[int, int, int]] = {}
    try:
        entries = _PROC.iterdir()
    except OSError:
        return table
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        parsed = parse_stat(int(name))
        if parsed is not None:
            table[int(name)] = parsed
    return table


def descendants_of(root: int, table: dict[int, tuple[int, int, int]] | None = None) -> set[int]:
    table = iter_process_table() if table is None else table
    children: dict[int, list[int]] = {}
    for pid, (ppid, _pgrp, _sid) in table.items():
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = list(children.get(root, ()))
    while stack:
        cur = stack.pop()
        if cur in found or cur == root:
            continue
        found.add(cur)
        stack.extend(children.get(cur, ()))
    return found


def proc_state(pid: int) -> str | None:
    """/proc/<pid>/stat 的状态字母；进程不存在时为 None。"""
    try:
        text = (_PROC / str(pid) / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    rparen = text.rfind(")")
    fields = text[rparen + 2 :].split() if rparen >= 0 else []
    return fields[0] if fields else None


def is_alive(pid: int) -> bool:
    """僵尸（已退出、只等父进程收尸，不再占 GPU/端口）不算存活；收尸仍由其父进程负责。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return proc_state(pid) not in (None, "Z", "X")


class ChildProcessGuard:
    """登记并回收本进程的子孙。新会话只在成功 setsid 后视为「本组」。"""

    def __init__(
        self,
        *,
        owner_pid: int | None = None,
        term_timeout_s: float = 5.0,
        kill_timeout_s: float = 2.0,
    ) -> None:
        self.owner_pid = int(owner_pid or os.getpid())
        self.term_timeout_s = term_timeout_s
        self.kill_timeout_s = kill_timeout_s
        self.invoker_pgid = os.getpgid(self.owner_pid)
        self.session_pgid: int | None = None
        self.entered_new_session = False
        self.owned: set[int] = set()
        self._reaped = False
        self._installed = False

    def enter_new_session(self) -> int:
        try:
            os.setsid()
            self.entered_new_session = True
        except OSError:
            # 已经是会话/组长时，本组仍归本进程所有；否则只能按子孙登记，不得动调用方进程组。
            self.entered_new_session = os.getpgrp() == self.owner_pid
        self.session_pgid = os.getpgrp()
        return self.session_pgid

    def remember(self, table: dict[int, tuple[int, int, int]] | None = None) -> set[int]:
        table = iter_process_table() if table is None else table
        found = descendants_of(self.owner_pid, table)
        if self.entered_new_session and self.session_pgid is not None:
            for pid, (_ppid, pgrp, _sid) in table.items():
                if pid != self.owner_pid and pgrp == self.session_pgid:
                    found.add(pid)
        self.owned |= found
        return set(self.owned)

    def reclaimable(self, table: dict[int, tuple[int, int, int]] | None = None) -> set[int]:
        table = iter_process_table() if table is None else table
        self.remember(table)
        targets: set[int] = set()
        for pid in self.owned:
            if pid == self.owner_pid or pid not in table:
                continue
            _ppid, pgrp, _sid = table[pid]
            if (
                self.entered_new_session
                and pgrp == self.invoker_pgid
                and pgrp != self.session_pgid
            ):
                continue
            targets.add(pid)
        return targets

    def reap(self) -> dict[str, list[int] | bool]:
        if self._reaped:
            return {"already": True, "term": [], "kill": [], "survived": []}
        self._reaped = True
        sent_term: list[int] = []
        sent_kill: list[int] = []
        # 多轮 remember，缩小 reap 前新子孙出生窗口；不按名字、不杀调用方进程组。
        for _round in range(3):
            targets = self.reclaimable() - set(sent_term) - set(sent_kill)
            if not targets:
                break
            for pid in sorted(targets):
                try:
                    os.kill(pid, signal.SIGTERM)
                    sent_term.append(pid)
                except (ProcessLookupError, PermissionError):
                    continue
            deadline = time.monotonic() + self.term_timeout_s
            while time.monotonic() < deadline and any(is_alive(pid) for pid in sent_term):
                time.sleep(0.05)
            still = [pid for pid in sent_term if is_alive(pid)]
            for pid in still:
                if pid in sent_kill:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                    sent_kill.append(pid)
                except (ProcessLookupError, PermissionError):
                    continue
            deadline = time.monotonic() + self.kill_timeout_s
            while time.monotonic() < deadline and any(is_alive(pid) for pid in sent_kill):
                time.sleep(0.05)
        survived = [pid for pid in sent_term if is_alive(pid)]
        return {"already": False, "term": sent_term, "kill": sent_kill, "survived": survived}

    def install(self, *, request_stop=None) -> None:
        if self._installed:
            return
        self._installed = True
        atexit.register(self.reap)

        def _handle(signum, _frame) -> None:
            if request_stop is not None:
                request_stop(signum)
                return
            self.remember()
            self.reap()
            os._exit(128 + int(signum))

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)


__all__ = [
    "ChildProcessGuard",
    "descendants_of",
    "is_alive",
    "iter_process_table",
    "parse_stat",
]
