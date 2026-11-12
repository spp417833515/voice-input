"""§8.2-6/7 平台→业务单元：截图译文流程、动作分派、录音失败可见、冲突扫描、启动检查、sd_notify。

全部假对象：不开选区、不截屏、不开麦、不读真实 gsettings、不连 systemd。
"""
from __future__ import annotations

import socket
import subprocess
import tempfile
from pathlib import Path

from _contracts import ErrorCode
from _desktop.actions import DesktopActions
from _desktop.screenshot import ScreenshotFlow
from _desktop.status import PlatformStatus
from _desktop.voice import VoiceFlow
from _focus import StaticFocusProbe
from _hotkey import HotkeyBindings
from _linux.conflicts import ConflictScan, scan_hotkey_conflicts
from _linux.screenshot_types import CaptureCallbacks
from _linux.sd_notify import sd_notify
from _linux.startup_check import startup_issues

DEFAULTS = HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", repeat="ctrl+alt+r")


def _status():
    posted: list[dict] = []
    return PlatformStatus(posted.append, start_thread=False), posted


class FakeCapture:
    def __init__(self) -> None:
        self.cb: CaptureCallbacks | None = None

    def start(self, cb: CaptureCallbacks) -> None:
        self.cb = cb

    def active(self) -> bool:
        return self.cb is not None


class FakeOverlay:
    def __init__(self, png, rect, events) -> None:
        self.png, self.rect, self.events = png, rect, events
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.events.closed()


class NullScheduler:
    def schedule(self, delay_ms, fn):
        return object()

    def cancel(self, handle) -> None:
        pass


def test_screenshot_flow_opens_overlay_with_image_and_rect():
    status, posted = _status()
    capture = FakeCapture()
    opened: list[FakeOverlay] = []

    def open_overlay(png, rect, events):
        opened.append(FakeOverlay(png, rect, events))
        return opened[-1]

    flow = ScreenshotFlow(capture=capture, open_overlay=open_overlay, status=status, scheduler=NullScheduler())
    flow.trigger()
    capture.cb.on_stage("selecting")
    capture.cb.on_captured(b"\x89PNG-fake", (10, 20, 300, 100))
    status.drain()
    # 选区中 busy；截好后 idle，之后的 busy/idle/error 由浮层页面对自己的请求上报
    assert [p["event"] for p in posted] == ["busy", "idle"]
    assert opened[0].png == b"\x89PNG-fake" and opened[0].rect == (10, 20, 300, 100)
    assert flow.overlay_active() is True
    # 任意键/Esc → close_overlay；再次截图先关旧浮层
    flow.trigger()
    assert opened[0].closed is True
    assert flow.overlay_active() is False


def test_screenshot_cancel_and_failure_are_visible():
    status, posted = _status()
    capture = FakeCapture()
    flow = ScreenshotFlow(capture=capture, open_overlay=lambda *a: None, status=status, scheduler=NullScheduler())
    flow.trigger()
    capture.cb.on_cancelled()
    flow.trigger()
    capture.cb.on_failed("portal 拒绝")
    status.drain()
    assert posted[0] == {"event": "idle"}
    assert posted[1]["event"] == "error" and posted[1]["error"] == ErrorCode.dependency_failed.value
    assert "portal" in posted[1]["message"]


def test_overlay_open_failure_is_visible():
    status, posted = _status()
    capture = FakeCapture()

    def boom(*a):
        raise RuntimeError("WebKit 缺失")

    flow = ScreenshotFlow(capture=capture, open_overlay=boom, status=status, scheduler=NullScheduler())
    flow.trigger()
    capture.cb.on_captured(b"png", (0, 0, 1, 1))
    status.drain()
    assert posted[-1]["event"] == "error" and "WebKit" in posted[-1]["message"]
    assert flow.overlay_active() is False


def test_voice_mic_failure_visible_and_returns_false():
    status, posted = _status()

    class Broken:
        def start(self, sink):
            raise OSError("PortAudio 设备不可用")

        def stop(self):
            pass

    calls: list[str] = []

    class Api:
        def create_session(self, **kw):
            calls.append("create")
            return "s"

    flow = VoiceFlow(api=Api(), status=status, probe=StaticFocusProbe(1), source_factory=Broken)
    assert flow.start() is False
    status.drain()
    assert posted[-1]["event"] == "error" and "麦克风" in posted[-1]["message"]
    assert calls == []  # 未开麦就不建会话


def test_voice_session_create_failure_visible():
    from _capture import FakeAudioSource

    status, posted = _status()

    class Api:
        def create_session(self, **kw):
            raise RuntimeError("ASR sidecar 未就绪")

    flow = VoiceFlow(api=Api(), status=status, probe=StaticFocusProbe(1), source_factory=lambda: FakeAudioSource(b"\x01\x00" * 16000))
    assert flow.start() is True
    flow.stop()
    flow.last_upload.join(5)
    status.drain()
    assert flow.last_upload.outcome == "failed"
    assert posted[-1]["error"] == ErrorCode.asr_unavailable.value


def test_desktop_actions_dispatch():
    log: list[str] = []

    class V:
        def start(self):
            log.append("vstart")
            return True

        def stop(self):
            log.append("vstop")

    class S:
        def trigger(self):
            log.append("shot")

        def overlay_active(self):
            return True

        def close_overlay(self):
            log.append("close")
            return False

    class R:
        def run(self):
            log.append("repeat")

    actions = DesktopActions(voice=V(), screenshot=S(), repeat=R(), spawn=lambda fn: fn())
    assert actions.voice_start() is True
    actions.voice_stop()
    actions.screenshot()
    actions.repeat()
    assert actions.overlay_active() is True
    actions.close_overlay()
    assert log == ["vstart", "vstop", "shot", "repeat", "close"]


def test_conflict_scan_with_fake_gsettings():
    def run(cmd, **kw):
        out = ""
        if cmd[:2] == ["gsettings", "list-recursively"] and cmd[2] == "org.gnome.desktop.wm.keybindings":
            out = "org.gnome.desktop.wm.keybindings show-desktop ['<Primary><Alt>d']\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    conflicts = scan_hotkey_conflicts(
        HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+d", repeat="ctrl+alt+r"),
        run=run,
        which=lambda n: "/usr/bin/gsettings",
    )
    assert any("show-desktop" in c for c in conflicts.conflicts), conflicts
    missing = scan_hotkey_conflicts(DEFAULTS, run=run, which=lambda n: None)
    assert missing.conflicts == () and missing.unread and "gsettings 不可用" in missing.unread[0]


def test_startup_issues_wayland_and_x11():
    env_w = {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0", "GDK_BACKEND": "x11"}
    issues = startup_issues(
        DEFAULTS, environ=env_w, which=lambda n: None, portal_version=lambda i: None,
        screenshot_tool=None, conflicts=lambda b: ConflictScan(conflicts=("c1",), unread=("u1",)),
    )
    assert any("xclip" in i for i in issues)
    assert any("org.freedesktop.portal.Screenshot" in i for i in issues)
    assert issues[-2] == "c1"
    assert "未能读取" in issues[-1] and "u1" in issues[-1]
    ok = startup_issues(
        DEFAULTS, environ={"XDG_SESSION_TYPE": "x11"}, which=lambda n: f"/usr/bin/{n}",
        portal_version=lambda i: 1, screenshot_tool="maim", conflicts=lambda b: ConflictScan((), ()),
    )
    assert ok == []


def test_sd_notify_noop_without_socket_and_sends_with_socket():
    assert sd_notify("READY=1", environ={}) is False
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "notify.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv.bind(path)
        try:
            assert sd_notify("WATCHDOG=1", environ={"NOTIFY_SOCKET": path}) is True
            assert srv.recv(64) == b"WATCHDOG=1"
        finally:
            srv.close()
