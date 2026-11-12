"""截图译文浮层生命周期（确定性假时钟/假窗口，不开 GTK/WebKit/截屏）。

原语义（备份 daemon.TranslationOverlay）：结果显示后 10s 关闭；Esc/窗外点击/任意键关闭；
30s 只是防挂上限 → 这里只约束“等待结果”阶段，到时显式 timeout 错误，不吃掉结果后的阅读时间。
“已呈现”只来自页面经本窗口桥发出的通知（events.presented），见 test_translate_presented_seam.py。
"""
from __future__ import annotations

import heapq
import json

from _contracts import ErrorCode
from _desktop.screenshot import ScreenshotFlow
from _desktop.status import PlatformStatus
from _desktop.translate_lifecycle import (
    PENDING_LIMIT_MS,
    RESULT_READ_MS,
    parse_presented_message,
)
from _linux.screenshot_types import CaptureCallbacks


class FakeClockScheduler:
    def __init__(self) -> None:
        self.now = 0
        self._seq = 0
        self._heap: list[tuple[int, int, object]] = []
        self._cancelled: set[int] = set()

    def schedule(self, delay_ms, fn):
        self._seq += 1
        heapq.heappush(self._heap, (self.now + int(delay_ms), self._seq, fn))
        return self._seq

    def cancel(self, handle) -> None:
        self._cancelled.add(handle)

    def pending(self) -> int:
        return sum(1 for _, seq, _ in self._heap if seq not in self._cancelled)

    def advance(self, ms: int) -> None:
        target = self.now + ms
        while self._heap and self._heap[0][0] <= target:
            when, seq, fn = heapq.heappop(self._heap)
            self.now = when
            if seq not in self._cancelled:
                fn()
        self.now = target


class FakeCapture:
    def __init__(self) -> None:
        self.cb: CaptureCallbacks | None = None

    def start(self, cb: CaptureCallbacks) -> None:
        self.cb = cb

    def active(self) -> bool:
        return self.cb is not None


class FakeWindow:
    """模拟 TranslateWindow：close()/用户 Esc 都走 destroy → events.closed()。"""

    def __init__(self, events) -> None:
        self.events = events
        self.destroyed = False
        self.destroy_calls = 0

    def close(self) -> bool:
        self.destroy_calls += 1
        if not self.destroyed:
            self.destroyed = True
            self.events.closed()
        return False

    user_escape = close  # Esc / 窗外点击在窗口层直接销毁


def _setup():
    clock = FakeClockScheduler()
    posted: list[dict] = []
    status = PlatformStatus(posted.append, start_thread=False)
    capture = FakeCapture()
    windows: list[FakeWindow] = []

    def open_overlay(png, rect, events):
        windows.append(FakeWindow(events))
        return windows[-1]

    flow = ScreenshotFlow(capture=capture, open_overlay=open_overlay, status=status, scheduler=clock)

    def shoot() -> FakeWindow:
        flow.trigger()
        capture.cb.on_captured(b"\x89PNG", (1, 2, 300, 100))
        return windows[-1]

    def events() -> list[dict]:
        status.drain()
        return list(posted)

    return clock, flow, shoot, events


def test_normal_result_closes_exactly_10s_after_visible():
    clock, flow, shoot, events = _setup()
    win = shoot()
    clock.advance(1_500)
    win.events.presented("result")
    clock.advance(RESULT_READ_MS - 1)
    assert not win.destroyed and flow.overlay_active()
    clock.advance(1)
    assert win.destroyed and not flow.overlay_active()
    assert [e["event"] for e in events()] == ["idle"]  # 截好即 idle；请求状态由页面自己上报
    assert clock.pending() == 0


def test_slow_result_after_29s_still_gets_full_10s_reading_time():
    clock, flow, shoot, events = _setup()
    win = shoot()
    clock.advance(29_000)
    assert not win.destroyed
    win.events.presented("result")
    clock.advance(9_999)  # 已过开窗 38.999s：旧“总 30s”会在 30s 关掉
    assert not win.destroyed
    clock.advance(1)
    assert win.destroyed
    assert [e["event"] for e in events()] == ["idle"]


def test_pending_limit_is_explicit_timeout_error_then_closeable_and_auto_closes():
    clock, flow, shoot, events = _setup()
    win = shoot()
    clock.advance(PENDING_LIMIT_MS)
    evs = events()
    assert evs[-1]["event"] == "error" and evs[-1]["error"] == ErrorCode.timeout.value
    assert not win.destroyed and flow.lifecycle.outcome == "timeout"  # 不伪装成功、错误可读
    # 超时后迟到的结果不续时、不改成功
    clock.advance(5_000)
    win.events.presented("result")
    clock.advance(RESULT_READ_MS - 5_000)
    assert win.destroyed
    assert [e["event"] for e in events()] == ["idle", "error"]


def test_request_error_is_visible_and_closes_10s_later():
    clock, flow, shoot, events = _setup()
    win = shoot()
    clock.advance(800)
    win.events.presented("error")
    life = flow.lifecycle
    assert life.state == "shown" and life.outcome == "error"
    # 页面已在浮层内显示错误并经 events/platform 报 error（真实错误码）；宿主不重复上报
    assert [e["event"] for e in events()] == ["idle"]
    clock.advance(RESULT_READ_MS - 1)
    assert not win.destroyed
    clock.advance(1)
    assert win.destroyed


def test_user_close_while_pending_then_late_result_does_not_reopen():
    clock, flow, shoot, events = _setup()
    win = shoot()
    clock.advance(3_000)
    win.user_escape()  # Esc / 窗外点击
    assert win.destroyed and not flow.overlay_active()
    win.events.presented("result")  # 迟到
    win.events.presented("error")
    clock.advance(60_000)
    assert win.destroy_calls == 1 and not flow.overlay_active()
    # 用户放弃等待：页面已销毁来不及报 idle，宿主撤掉页面报的“处理中”；无迟到错误
    assert [e["event"] for e in events()] == ["idle", "idle"]
    assert clock.pending() == 0


def test_any_key_close_via_flow_then_late_result_ignored():
    clock, flow, shoot, events = _setup()
    win = shoot()
    win.events.presented("result")
    clock.advance(2_000)
    flow.close_overlay()  # 热键层任意键 → GLib.idle_add(close_overlay)
    assert win.destroyed
    win.events.presented("result")
    clock.advance(30_000)
    assert win.destroy_calls == 1
    assert clock.pending() == 0


def test_repeated_results_do_not_extend_timer():
    clock, flow, shoot, events = _setup()
    win = shoot()
    win.events.presented("result")
    clock.advance(6_000)
    win.events.presented("result")
    win.events.presented("error")
    win.events.presented("bogus")
    clock.advance(3_999)
    assert not win.destroyed
    clock.advance(1)
    assert win.destroyed
    assert [e["event"] for e in events()] == ["idle"]


def test_old_overlay_late_events_do_not_touch_new_overlay():
    clock, flow, shoot, events = _setup()
    first = shoot()
    second = shoot()  # 新截图先关旧浮层
    assert first.destroyed and not second.destroyed
    first.events.presented("result")  # 旧页面迟到
    clock.advance(RESULT_READ_MS)
    assert not second.destroyed and flow.lifecycle.state == "pending"
    second.events.presented("result")
    clock.advance(RESULT_READ_MS)
    assert second.destroyed


URL = "https://127.0.0.1:17945/overlay-translate"
GEN = "g" * 32


def _msg(**over):
    body = {"v": 1, "generation": GEN, "state": "result"}
    body.update(over)
    return json.dumps(body)


def test_presented_message_strict_whitelist():
    ok = dict(generation=GEN, page_uri=URL, expected_url=URL)
    assert parse_presented_message(_msg(), **ok) == "result"
    assert parse_presented_message(_msg(state="error"), **ok) == "error"
    assert parse_presented_message(_msg(state="loading"), **ok) is None
    assert parse_presented_message(_msg(generation="h" * 32), **ok) is None  # 旧窗口/别的窗口
    assert parse_presented_message(_msg(v=True), **ok) is None
    assert parse_presented_message(_msg(v=2), **ok) is None
    extra = json.dumps({"v": 1, "generation": GEN, "state": "result", "text": "OCR 原文"})
    assert parse_presented_message(extra, **ok) is None  # 不接受携带文本/多余字段
    assert parse_presented_message("not json", **ok) is None
    assert parse_presented_message(None, **ok) is None
    assert parse_presented_message(json.dumps(["result"]), **ok) is None
    assert parse_presented_message(_msg() + " " * 300, **ok) is None  # 超长
    # 只认本窗口当前的本机译文页 URI
    assert parse_presented_message(_msg(), generation=GEN, page_uri="https://127.0.0.1:17945/overlay-status", expected_url=URL) is None
    assert parse_presented_message(_msg(), generation=GEN, page_uri="https://192.168.1.5:17945/overlay-translate", expected_url=URL) is None
    assert parse_presented_message(_msg(), generation=GEN, page_uri="https://127.0.0.1:9999/overlay-translate", expected_url=URL) is None
    assert parse_presented_message(_msg(), generation=GEN, page_uri=None, expected_url=URL) is None
    assert parse_presented_message(_msg(), generation=GEN, page_uri=URL + "?x=1#h", expected_url=URL) == "result"


def test_generation_script_is_readonly_and_validated():
    import pytest

    from _overlay import translate_generation_script

    js = translate_generation_script("A" * 32)
    assert "writable:false" in js and "configurable:false" in js and "A" * 32 in js
    for bad in ("short", "x" * 65, "a" * 20 + "'", "a" * 20 + "\n"):
        with pytest.raises(ValueError):
            translate_generation_script(bad)


_WEBKIT_PROBE = """
import warnings
import gi
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")
from gi.repository import GObject, WebKit2
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    GObject.type_class_ref(WebKit2.UserContentManager.__gtype__)
ucm = set(GObject.signal_list_names(WebKit2.UserContentManager.__gtype__))
assert "script-message-received" in ucm, ucm
q = GObject.signal_query(GObject.signal_lookup("script-message-received", WebKit2.UserContentManager))
assert [t.name for t in q.param_types] == ["WebKitJavascriptResult"], q.param_types
for cls, name in [(WebKit2.UserContentManager, "register_script_message_handler"),
                  (WebKit2.UserContentManager, "unregister_script_message_handler"),
                  (WebKit2.UserContentManager, "add_script"),
                  (WebKit2.JavascriptResult, "get_js_value"),
                  (WebKit2.WebView, "get_uri"),
                  (WebKit2.UserScript, "new")]:
    assert hasattr(cls, name), (cls, name)
from gi.repository import JavaScriptCore
assert hasattr(JavaScriptCore.Value, "is_string") and hasattr(JavaScriptCore.Value, "to_string")
print("webkit-api-ok")
"""


def test_webkit_api_used_by_translate_window_exists():
    """窗口层私有桥所用的 WebKit2 4.1 信号/方法真实存在（只做类型自省，不建窗口、不连显示）。

    测试 venv 没有 gi 时用生产 .venv 解释器在子进程里自省（与 test_platform_imports 同法）。
    """
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    try:
        import gi  # noqa: F401

        python = sys.executable
    except ImportError:
        prod = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"
        if not prod.exists():
            pytest.skip("无 gi 且无生产 .venv")
        python = str(prod)
    out = subprocess.run(
        [python, "-c", _WEBKIT_PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
    )
    if "No module named 'gi'" in out.stderr or "Namespace WebKit2 not available" in out.stderr:
        pytest.skip("WebKit2 typelib 不可用")
    assert out.returncode == 0 and "webkit-api-ok" in out.stdout, out.stderr[-800:]
