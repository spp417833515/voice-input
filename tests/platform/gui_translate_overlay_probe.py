"""真实 WebKit 译文浮层 GUI 探针（只在 Xvfb 隔离显示上运行；由 test_translate_overlay_gui.py 以
xvfb-run + 生产 .venv 解释器启动）。

隔离：随机回环端口 + 临时自签证书（TLS 校验开启）+ 临时数据根 + 假 OCR/翻译；真实 static/v3 页面、
真实 TranslateWindow / WebKit 私有桥 / ScreenshotFlow / TranslateLifecycle / GLibScheduler（真实 10s）。
不用用户显示（要求 OVI_XVFB=1）、不碰剪贴板/键鼠/麦克风、不连 17945/17946/11434。
输出一行 JSON 时序证据。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

if os.environ.get("OVI_XVFB") != "1" or not os.environ.get("DISPLAY"):
    raise SystemExit("拒绝：只能在 xvfb-run 隔离显示上运行（OVI_XVFB=1）")
os.environ.pop("WAYLAND_DISPLAY", None)

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from _contracts import TranslateResponse  # noqa: E402
from _desktop import screenshot as shot_mod  # noqa: E402
from _desktop.screenshot import ScreenshotFlow  # noqa: E402
from _linux.glib_scheduler import GLibScheduler  # noqa: E402
from _linux.overlay_windows import TranslateWindow  # noqa: E402
from _linux.screenshot_types import CaptureCallbacks  # noqa: E402
from _v3.isolated import FORBIDDEN_PORTS, compose_isolated_candidate  # noqa: E402

SCENARIO = sys.argv[1]
T0 = time.monotonic()
EV: dict = {"scenario": SCENARIO, "requests": [], "dom_samples": []}
RELEASE = threading.Event()


def now() -> float:
    return round(time.monotonic() - T0, 3)


def self_signed(tmp: Path):
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", str(key),
         "-out", str(cert), "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True, timeout=60,
    )
    return cert, key


class ProbeMiddleware:
    """只在本隔离服务器上：记录请求；idle_pending 场景挂起 platform idle 响应；config_fail 场景让配置 500。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method, path = scope["method"], scope["path"]
        chunks = []
        more = True
        while more:
            msg = await receive()
            chunks.append(msg)
            more = msg.get("more_body", False)
        body = b"".join(m.get("body", b"") for m in chunks)
        rec = {"t": now(), "method": method, "path": path}
        if path == "/api/v1/events/platform":
            try:
                rec["event"] = json.loads(body).get("event")
            except Exception:
                pass
        EV["requests"].append(rec)
        if SCENARIO == "config_fail" and method == "GET" and path == "/api/v1/config":
            payload = json.dumps({"ok": False, "error": "internal", "message": "探针：配置读取失败"}).encode()
            await send({"type": "http.response.start", "status": 500, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
            return
        if SCENARIO == "idle_pending" and rec.get("event") == "idle":
            while not RELEASE.is_set():
                await asyncio.sleep(0.05)
            rec["responded"] = now()
        it = iter(chunks)

        async def replay():
            try:
                return next(it)
            except StopIteration:
                return {"type": "http.disconnect"}

        return await self.app(scope, replay, send)


def dom_probe_js() -> str:
    return (
        "JSON.stringify({phase:(window.__v3Translate&&window.__v3Translate.getState().phase)||null,"
        "text:(document.querySelector('.block-text')||{}).textContent||null,"
        "alert:!!document.querySelector('[role=alert]'),"
        "gen:typeof window.__V3_TRANSLATE_GENERATION__})"
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ovi-gui-probe-"))
    cert, key = self_signed(tmp)
    comp = compose_isolated_candidate(
        root=tmp / "iso",
        token="gui-probe-token",
        cert=cert,
        key=key,
        ocr=lambda payload, **kw: "你好",
        translate=lambda req: TranslateResponse(text="Hello", target_lang=req.target_lang),
        mount_static=True,
        fixture_notes=("gui probe：假 OCR/翻译，Xvfb",),
    )
    assert comp.bind_port not in FORBIDDEN_PORTS
    comp.app.add_middleware(ProbeMiddleware)
    base = comp.start_loopback_https()
    EV["origin_port_random"] = comp.bind_port

    windows: list[TranslateWindow] = []
    posted: list[dict] = []

    class Status:
        def busy(self):
            posted.append({"t": now(), "event": "busy"})

        def idle(self):
            posted.append({"t": now(), "event": "idle"})

        def error(self, code, message):
            posted.append({"t": now(), "event": "error", "error": str(code)})

    orig_presented = shot_mod.OverlayEvents.presented
    orig_closed = shot_mod.OverlayEvents.closed

    def presented(self, kind):
        EV.setdefault("presented", []).append({"t": now(), "kind": kind})
        win = windows[-1]

        def got(webview, result):
            try:
                EV["dom_at_presented"] = json.loads(webview.evaluate_javascript_finish(result).to_string())
            except Exception as exc:
                EV["dom_at_presented"] = {"error": str(exc)}

        win.host.webview.evaluate_javascript(dom_probe_js(), -1, None, None, None, got)
        return orig_presented(self, kind)

    def closed(self):
        EV["closed"] = now()
        RELEASE.set()
        GLib.timeout_add(800, lambda: (Gtk.main_quit(), False)[1])
        return orig_closed(self)

    shot_mod.OverlayEvents.presented = presented
    shot_mod.OverlayEvents.closed = closed

    class Capture:
        cb = None

        def start(self, cb: CaptureCallbacks):
            Capture.cb = cb

        def active(self):
            return False

    def open_overlay(png, rect, events):
        win = TranslateWindow(origin=base, cert=cert, png=png, rect=rect, events=events)
        windows.append(win)
        EV["opened"] = now()
        return win

    flow = ScreenshotFlow(capture=Capture(), open_overlay=open_overlay, status=Status(), scheduler=GLibScheduler())
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )

    def kick():
        flow.trigger()
        Capture.cb.on_captured(png, (40, 40, 360, 160))
        return False

    def sample():
        if not windows or windows[-1].host.webview is None:
            return True

        def got(webview, result):
            try:
                d = json.loads(webview.evaluate_javascript_finish(result).to_string())
                d["t"] = now()
                EV["dom_samples"].append(d)
            except Exception:
                pass

        windows[-1].host.webview.evaluate_javascript(dom_probe_js(), -1, None, None, None, got)
        return True

    GLib.idle_add(kick)
    GLib.timeout_add(100, sample)
    GLib.timeout_add(45_000, lambda: (EV.setdefault("hard_timeout", True), Gtk.main_quit(), False)[2])
    Gtk.main()
    RELEASE.set()
    EV["host_status"] = posted
    comp.close()
    # 只保留首次进入 result/error 的 DOM 采样时间，避免输出过长
    first = {}
    for s in EV["dom_samples"]:
        for key in ("result", "error"):
            if s.get("phase") == key and key not in first:
                first[key] = s
    EV["dom_first"] = first
    EV["dom_samples"] = len(EV["dom_samples"])
    print("PROBE_JSON " + json.dumps(EV, ensure_ascii=False))


if __name__ == "__main__":
    main()
