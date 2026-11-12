"""译文页 ↔ 宿主“结果已呈现”接缝的确定性跨界测试（无 gi、无网络、无 GUI）。

页面半边：node 在 vm 中加载真实 static/v3 业务脚本（translate_presented_seam.js），输出逻辑时序与
假 WebKit 桥上真实发出的原始字符串。宿主半边：同一字符串交给真实 parse_presented_message
（窗口层用的同一校验）与真实 ScreenshotFlow/TranslateLifecycle，假时钟按页面时序推进。
真实 WebKit DOM 证据见 scripts/probe_translate_overlay_gui.py（Xvfb 隔离，另行运行）。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from _desktop.screenshot import ScreenshotFlow
from _desktop.status import PlatformStatus
from _desktop.translate_lifecycle import PENDING_LIMIT_MS, RESULT_READ_MS, parse_presented_message
from tests.platform.test_translate_lifecycle import FakeCapture, FakeClockScheduler

HERE = Path(__file__).resolve().parent
URL = "https://127.0.0.1:17945/overlay-translate"
GEN_A = "A" * 32
GEN_B = "B" * 32
STEP_MS = 100  # 页面每个逻辑步骤在宿主假时钟上推进的毫秒数


def _node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    cands = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")))
    return cands[-1] if cands else None


@pytest.fixture(scope="module")
def page_runs() -> dict:
    node = _node()
    if node is None:
        pytest.skip("没有 node")
    out = subprocess.run(
        [node, str(HERE / "translate_presented_seam.js")], capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(out.stdout)


class HostWindow:
    """与 _linux/overlay_windows.TranslateWindow 同一路径：close → destroy → events.closed；
    页面消息经 parse_presented_message 校验后才交给 events.presented。"""

    def __init__(self, events, generation: str) -> None:
        self.events = events
        self.generation = generation
        self.destroyed = False

    def close(self) -> bool:
        if not self.destroyed:
            self.destroyed = True
            self.events.closed()
        return False

    def on_page_message(self, raw: str | None, page_uri: str = URL) -> None:
        if self.destroyed:
            return  # 已注销桥
        kind = parse_presented_message(raw, generation=self.generation, page_uri=page_uri, expected_url=URL)
        if kind is not None:
            self.events.presented(kind)


def _host(generation: str = GEN_A):
    clock = FakeClockScheduler()
    posted: list[dict] = []
    status = PlatformStatus(posted.append, start_thread=False)
    capture = FakeCapture()
    windows: list[HostWindow] = []
    gens = iter([generation, GEN_B, "C" * 32])

    def open_overlay(_png, _rect, events):
        windows.append(HostWindow(events, next(gens)))
        return windows[-1]

    flow = ScreenshotFlow(capture=capture, open_overlay=open_overlay, status=status, scheduler=clock)

    def shoot() -> HostWindow:
        flow.trigger()
        capture.cb.on_captured(b"\x89PNG", (0, 0, 10, 10))
        return windows[-1]

    def events() -> list[dict]:
        status.drain()
        return list(posted)

    return clock, flow, shoot, events


def _replay(log: list[dict], clock: FakeClockScheduler, win: HostWindow) -> dict[str, int]:
    """按页面逻辑时序推进宿主假时钟；桥消息原样交给宿主。返回关键时刻。"""
    marks: dict[str, int] = {}
    for entry in log:
        clock.advance(STEP_MS)
        k = entry["k"]
        if k == "fetch" and entry["path"] == "/api/v1/ocr-translate":
            marks.setdefault("ocr_sent", clock.now)
        elif k == "dom" and entry["phase"] in ("result", "error"):
            marks.setdefault("dom_" + entry["phase"], clock.now)
        elif k == "bridge":
            marks.setdefault("bridge", clock.now)
            win.on_page_message(entry["raw"])
        elif k == "fetch" and entry["path"] == "/api/v1/events/platform":
            marks.setdefault("platform_" + entry["body"]["event"], clock.now)
    return marks


def _bridges(log):
    return [e["raw"] for e in log if e["k"] == "bridge"]


def test_api_2xx_with_idle_pending_does_not_start_10s_early(page_runs):
    log = page_runs["idle_pending"]
    kinds = [(e["k"], e.get("phase") or e.get("path")) for e in log]
    i_dom = kinds.index(("dom", "result"))
    i_bridge = next(i for i, e in enumerate(log) if e["k"] == "bridge")
    i_idle = next(i for i, e in enumerate(log) if e["k"] == "fetch" and e.get("body", {}).get("event") == "idle")
    assert i_dom < i_bridge < i_idle, "DOM 先更新、再通知宿主；idle 上报（挂起）不阻塞 DOM"
    assert json.loads(_bridges(log)[0]) == {"v": 1, "generation": GEN_A, "state": "result"}
    assert "Hello" not in _bridges(log)[0] and "你好" not in _bridges(log)[0]

    clock, flow, shoot, events = _host()
    win = shoot()
    # 宿主侧：ocr 2xx 已返回很久（模拟页面渲染被延后 4s），未收到桥之前绝不开始 10s
    marks = _replay([e for e in log if e["k"] != "bridge"], clock, win)
    clock.advance(4_000)
    assert flow.lifecycle.state == "pending" and not win.destroyed
    t_bridge = clock.now
    win.on_page_message(_bridges(log)[0])
    assert marks["ocr_sent"] < t_bridge
    clock.advance(RESULT_READ_MS - 1)
    assert not win.destroyed
    clock.advance(1)
    assert win.destroyed and clock.now == t_bridge + RESULT_READ_MS


def test_rendered_result_closes_exactly_10s_after_bridge(page_runs):
    clock, flow, shoot, events = _host()
    win = shoot()
    marks = _replay(page_runs["idle_pending"], clock, win)
    assert marks["dom_result"] < marks["bridge"]
    remaining = marks["bridge"] + RESULT_READ_MS - clock.now
    clock.advance(remaining - 1)
    assert not win.destroyed
    clock.advance(1)
    assert win.destroyed
    assert [e["event"] for e in events()] == ["idle"]  # 宿主不重复报页面已报的状态


def test_config_init_failure_notifies_error_without_waiting_30s(page_runs):
    log = page_runs["config_fail"]
    assert not any(e["k"] == "fetch" and e["path"] == "/api/v1/ocr-translate" for e in log)
    assert [json.loads(r)["state"] for r in _bridges(log)] == ["error"]
    clock, flow, shoot, events = _host()
    win = shoot()
    marks = _replay(log, clock, win)
    assert marks["dom_error"] < marks["bridge"] < 1_000
    clock.advance(marks["bridge"] + RESULT_READ_MS - clock.now)
    assert win.destroyed and clock.now < PENDING_LIMIT_MS
    assert all(e.get("error") != "timeout" for e in events())


@pytest.mark.parametrize("name", ["ocr_error", "network_exception", "openapi_fail"])
def test_every_visible_error_notifies_once(page_runs, name):
    log = page_runs[name]
    assert [json.loads(r)["state"] for r in _bridges(log)] == ["error"], log
    i_dom = next(i for i, e in enumerate(log) if e["k"] == "dom" and e["phase"] == "error")
    i_bridge = next(i for i, e in enumerate(log) if e["k"] == "bridge")
    assert i_dom < i_bridge
    clock, flow, shoot, _events = _host()
    win = shoot()
    _replay(log, clock, win)
    assert flow.lifecycle.state == "shown" and flow.lifecycle.outcome == "error"


def test_ocr_error_keeps_real_code_in_dom_and_platform(page_runs):
    log = page_runs["ocr_error"]
    dom_err = next(e for e in log if e["k"] == "dom" and e["phase"] == "error")
    plat = next(e for e in log if e["k"] == "fetch" and e.get("body", {}).get("event") == "error")
    assert dom_err["error"] == plat["body"]["error"] == "translate_failed"
    # 之后再次呈现（重复/迟到）不再发第二次通知
    assert any(e["k"] == "dom" and e["phase"] == "result" for e in log[log.index(dom_err) + 1 :])
    assert len(_bridges(log)) == 1


def test_no_bridge_page_still_renders_and_host_times_out_explicitly(page_runs):
    log = page_runs["no_bridge"]
    assert _bridges(log) == [] and any(e["k"] == "dom" and e["phase"] == "result" for e in log)
    assert not any(e["k"] == "exception" for e in log)
    clock, flow, shoot, events = _host()
    win = shoot()
    _replay(log, clock, win)
    clock.advance(PENDING_LIMIT_MS - clock.now)
    assert events()[-1]["error"] == "timeout" and not win.destroyed
    clock.advance(RESULT_READ_MS)
    assert win.destroyed


def test_pagehide_blocks_late_notification(page_runs):
    log = page_runs["pagehide"]
    i_hide = next(i for i, e in enumerate(log) if e["k"] == "pagehide")
    assert any(e["k"] == "dom" and e["phase"] == "result" for e in log[i_hide:])
    assert _bridges(log) == []


def test_old_window_and_new_screenshot_isolated(page_runs):
    raw_a, raw_b = _bridges(page_runs["two_windows"])
    clock, flow, shoot, events = _host(generation=GEN_A)
    first = shoot()
    second = shoot()  # 新截图：旧窗关闭（桥注销），新窗 generation=GEN_B
    assert first.destroyed and not second.destroyed
    first.on_page_message(raw_a)  # 旧窗迟到
    second.on_page_message(raw_a)  # 旧 generation 投到新窗
    clock.advance(RESULT_READ_MS * 2)
    assert flow.lifecycle.state == "pending" and not second.destroyed
    # 非本窗口 URI（例如局域网页面）即使拿到正确 generation 也被拒
    second.on_page_message(raw_b, page_uri="https://192.168.1.9:17945/overlay-translate")
    assert flow.lifecycle.state == "pending"
    second.on_page_message(raw_b)
    t = clock.now
    second.on_page_message(raw_b)  # 重复不续期
    clock.advance(RESULT_READ_MS)
    assert second.destroyed and clock.now == t + RESULT_READ_MS
