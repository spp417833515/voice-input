"""粘贴后端与剪贴板回收（单一来源 `_linux.paste_backend`）。全部用假剪贴板/假进程/假键盘，不碰用户剪贴板。"""
from __future__ import annotations

import subprocess

from _inject import ack_from_evidence
from _linux.inject_guard import InjectGuard
from _linux.paste_backend import LinuxPasteBackend


class FakeClipboard:
    """测试专用剪贴板：记录读写；可让写失败或在粘贴后被“用户”改写。"""

    def __init__(self, initial: str = "用户原剪贴板") -> None:
        self.value = initial
        self.writes: list[str] = []
        self.fail_write: str | None = None
        self.on_paste_user_copies: str | None = None

    def write(self, text: str) -> str | None:
        if self.fail_write is not None:
            return self.fail_write
        self.writes.append(text)
        self.value = text
        return None

    def read(self) -> str:
        return self.value


class FakeJanitor:
    uinput = None
    kb = None
    Key = None

    def __init__(self) -> None:
        self.waited = 0
        self.cleared: list[str] = []

    def wait_released(self) -> None:
        self.waited += 1

    def clear_uinput(self) -> None:
        self.cleared.append("uinput")

    def clear_pynput(self) -> None:
        self.cleared.append("pynput")


class FakeKey:
    ctrl = "ctrl"
    shift = "shift"


class FakeKb:
    def __init__(self, fail_on_tap: bool = False) -> None:
        self.events: list[tuple[str, str]] = []
        self.fail_on_tap = fail_on_tap

    def press(self, k):
        self.events.append(("press", k))

    def release(self, k):
        self.events.append(("release", k))

    def tap(self, k):
        if self.fail_on_tap:
            raise RuntimeError("tap boom")
        self.events.append(("tap", k))


def _backend(clip, *, session="x11", run=None, which=None, janitor=None):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if clip.on_paste_user_copies is not None:
            clip.value = clip.on_paste_user_copies
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend = LinuxPasteBackend(
        session_type=session,
        guard=InjectGuard(),
        janitor=janitor or FakeJanitor(),
        clipboard=clip,
        run=run or fake_run,
        which=which or (lambda name: f"/usr/bin/{name}" if name == "xdotool" else None),
        sleep=lambda s: None,
    )
    return backend, calls


def test_xdotool_paste_complete_and_clipboard_restored():
    clip = FakeClipboard()
    backend, calls = _backend(clip)
    ev = backend.deliver("你好")
    assert ev.method == "xdotool" and ev.proved_output
    assert ack_from_evidence(ev) == "complete"
    assert calls == [["/usr/bin/xdotool", "key", "ctrl+v"]]
    assert clip.value == "用户原剪贴板"  # 已回收
    assert clip.writes == ["你好", "用户原剪贴板"]


def test_terminal_uses_ctrl_shift_v():
    clip = FakeClipboard()
    backend, calls = _backend(clip)
    backend.deliver("ls", terminal=True)
    assert calls == [["/usr/bin/xdotool", "key", "ctrl+shift+v"]]


def test_user_copy_during_paste_not_overwritten():
    clip = FakeClipboard()
    clip.on_paste_user_copies = "用户新复制"
    backend, _ = _backend(clip)
    ev = backend.deliver("你好")
    assert ack_from_evidence(ev) == "complete"
    assert clip.value == "用户新复制"


def test_clipboard_write_failure_proves_none():
    clip = FakeClipboard()
    clip.fail_write = "xclip 不可用"
    backend, calls = _backend(clip)
    ev = backend.deliver("你好")
    assert ack_from_evidence(ev) == "none"
    assert calls == []


def test_no_channel_proves_none_and_restores():
    clip = FakeClipboard()
    backend, _ = _backend(clip, which=lambda name: None)
    ev = backend.deliver("你好")
    assert ack_from_evidence(ev) == "none"
    assert clip.value == "用户原剪贴板"


def test_midway_failure_is_partial_or_unknown_not_retried():
    clip = FakeClipboard()
    janitor = FakeJanitor()
    janitor.kb = FakeKb(fail_on_tap=True)
    janitor.Key = FakeKey
    backend, calls = _backend(clip, which=lambda name: None, janitor=janitor)
    ev = backend.deliver("你好")
    assert ack_from_evidence(ev) == "partial_or_unknown"
    # 修饰键配对释放
    assert ("release", "ctrl") in janitor.kb.events
    assert "pynput" in janitor.cleared
    assert clip.value == "用户原剪贴板"


def test_wayland_wtype_ctrl_shift_v():
    clip = FakeClipboard()
    backend, calls = _backend(clip, session="wayland", which=lambda name: f"/usr/bin/{name}" if name == "wtype" else None)
    ev = backend.deliver("你好")
    assert ev.method == "wtype"
    assert calls[0][:5] == ["/usr/bin/wtype", "-M", "ctrl", "-M", "shift"]


def test_ibus_commit_short_circuits_clipboard():
    class FakeIBus:
        def commit(self, text):
            return True

    clip = FakeClipboard()
    backend, calls = _backend(clip)
    backend.ibus = FakeIBus()
    ev = backend.deliver("你好")
    assert ev.method == "ibus" and ack_from_evidence(ev) == "complete"
    assert clip.writes == [] and calls == []


def test_inject_guard_window_open_during_delivery():
    clip = FakeClipboard()
    seen = {}

    def run(cmd, **kw):
        seen["active"] = backend.guard.active()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend, _ = _backend(clip, run=run)
    backend.deliver("x")
    assert seen["active"] is True
