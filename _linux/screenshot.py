"""截图采集：Wayland 走 portal 整屏 + 自绘冻结选区；X11 走 maim 选区。

只负责拿到"选区 PNG 字节 + 选区屏幕矩形"，不调用 OCR/翻译接口（那是浮层页面的职责）。
回调都在 GTK 主线程派发。连按用 generation 作废旧请求，临时文件总被删除。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import threading
import traceback
from pathlib import Path

from gi.repository import GLib

from _linux.geometry import png_dimensions
from _linux.portal_dbus import SCREENSHOT_IFACE, portal_interface_version
from _linux.screenshot_types import CaptureCallbacks


def detect_screenshot_tool(environ=None, which=shutil.which, portal_version=portal_interface_version) -> str | None:
    """按能力选择截图后端：Wayland 只认 xdg-desktop-portal，X11 用 maim。"""
    env = os.environ if environ is None else environ
    session = env.get("XDG_SESSION_TYPE", "x11")
    if session == "wayland":
        # GNOME 不支持 wlr-screencopy / wlr-layer-shell；grim/slurp 一律不选。
        if portal_version(SCREENSHOT_IFACE) is not None:
            return "portal"
        return None
    if which("maim"):
        return "x11-maim"
    if which("gnome-screenshot"):
        return "gnome"
    return None


def mouse_position() -> tuple[int, int] | None:
    """获取当前鼠标位置（可在非 GTK 线程调用）。"""
    try:
        from Xlib import display as xdisplay

        dpy = xdisplay.Display()
        try:
            qp = dpy.screen().root.query_pointer()
            return (qp.root_x, qp.root_y)
        finally:
            dpy.close()
    except Exception:
        return None


class ScreenCapture:
    def __init__(self, *, tool: str | None) -> None:
        self.tool = tool
        self._generation = 0
        self._busy = False
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._client = None
        self._selection = None
        self._parent = None

    def active(self) -> bool:
        """截图/选区是否仍在进行。"""
        proc = self._proc
        return bool(self._busy or self._selection is not None or (proc is not None and proc.poll() is None))

    def start(self, cb: CaptureCallbacks) -> None:
        if self.tool == "portal":
            self._start_portal(cb)
            return
        if not self.tool:
            cb.on_failed("截图不可用：Wayland 需要 xdg-desktop-portal，X11 需要 maim")
            return
        self._start_x11(cb)

    # ---- X11 maim -----------------------------------------------------------
    def _start_x11(self, cb: CaptureCallbacks) -> None:
        # 取消上一次仍在跑的 maim
        with self._lock:
            prev = self._proc
        if prev is not None and prev.poll() is None:
            try:
                prev.terminate()
            except Exception:
                pass
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        if self.tool == "x11-maim":
            cmd = ["maim", "-s", tmp_path]
        else:  # gnome-screenshot
            cmd = ["gnome-screenshot", "-a", "-f", tmp_path]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with self._lock:
            self._proc = proc
        self._busy = True
        cb.on_stage("截图选择中")
        threading.Thread(target=self._wait_x11, args=(proc, tmp_path, cb), daemon=True).start()

    def _wait_x11(self, proc: subprocess.Popen, tmp_path: str, cb: CaptureCallbacks) -> None:
        try:
            if proc.wait() != 0:
                GLib.idle_add(_once(cb.on_cancelled))
                return
            # 选区几何：图片尺寸 + 鼠标释放位置（释放点通常是选区右下角）
            img_w, img_h = png_dimensions(tmp_path)
            pos = mouse_position()
            if pos:
                sel_x, sel_y = max(0, pos[0] - img_w), max(0, pos[1] - img_h)
            else:
                sel_x, sel_y = 100, 100
            data = Path(tmp_path).read_bytes()
            GLib.idle_add(_once(cb.on_captured, data, (sel_x, sel_y, img_w, img_h)))
        except Exception as exc:
            traceback.print_exc()
            GLib.idle_add(_once(cb.on_failed, f"截图失败: {str(exc)[:60]}"))
        finally:
            Path(tmp_path).unlink(missing_ok=True)
            with self._lock:
                # 仅清自己启动的那个 proc 引用; 若已被新触发覆盖则保留新的
                if self._proc is proc:
                    self._proc = None
            self._busy = False

    # ---- Wayland portal ------------------------------------------------------
    def _start_portal(self, cb: CaptureCallbacks) -> None:
        from _linux.portal_screenshot import PortalParentWindow, PortalScreenshotClient

        self._generation += 1
        gen = self._generation
        self._busy = True
        self._close_selection()
        if self._parent is None:
            self._parent = PortalParentWindow()
        self._parent.close()
        if self._client is not None:
            try:
                self._client.cleanup()
            except Exception:
                pass
            self._client = None
        cb.on_stage("截图中")
        try:
            client = PortalScreenshotClient()
        except Exception as exc:
            self._busy = False
            cb.on_failed(f"截图失败: {str(exc)[:60]}")
            return
        self._client = client

        def on_ok(path: str) -> None:
            if gen != self._generation:
                Path(path).unlink(missing_ok=True)
                return
            self._parent.close()
            self._show_selection(path, gen, cb)

        def on_error(message: str) -> None:
            if gen != self._generation:
                return
            self._parent.close()
            self._busy = False
            cb.on_failed(f"截图失败: {message[:60]}")

        def start_capture(parent: str) -> None:
            # 父窗口 ready 之前可能已被新触发取代；过期请求不再发 Screenshot。
            if gen != self._generation:
                return
            client.capture(on_ok, on_error, parent)

        self._parent.open(start_capture)

    def _show_selection(self, path: str, gen: int, cb: CaptureCallbacks) -> None:
        from _linux.selection import SelectionOverlay

        self._close_selection()
        cb.on_stage("拖拽选择区域")

        def selected(img_box, screen_rect) -> None:
            self._selection = None
            if gen != self._generation:
                Path(path).unlink(missing_ok=True)
                return
            rect = tuple(int(v) for v in screen_rect)

            def work() -> None:
                # 解码整屏 PNG 并裁剪可能耗时，放后台线程，避免阻塞 GTK 主循环。
                try:
                    data = _crop_png(path, img_box)
                except Exception as exc:
                    traceback.print_exc()
                    self._busy = False
                    GLib.idle_add(_once(cb.on_failed, f"裁剪失败: {str(exc)[:60]}"))
                    return
                finally:
                    Path(path).unlink(missing_ok=True)
                self._busy = False
                if gen == self._generation:
                    GLib.idle_add(_once(cb.on_captured, data, rect))

            threading.Thread(target=work, daemon=True).start()

        def cancelled() -> None:
            Path(path).unlink(missing_ok=True)
            self._selection = None
            if gen != self._generation:
                return
            self._busy = False
            cb.on_cancelled()

        try:
            self._selection = SelectionOverlay(path, on_selected=selected, on_cancel=cancelled)
        except Exception as exc:
            traceback.print_exc()
            Path(path).unlink(missing_ok=True)
            self._busy = False
            cb.on_failed(f"选区窗口失败: {str(exc)[:60]}")

    def _close_selection(self) -> None:
        """静默关闭旧选区窗口，并删除它持有的临时截图。"""
        overlay = self._selection
        self._selection = None
        if overlay is None:
            return
        path = getattr(overlay, "image_path", None)
        try:
            overlay.close_silent()
        except Exception:
            pass
        if path:
            Path(path).unlink(missing_ok=True)

    def cleanup(self) -> None:
        self._close_selection()
        if self._parent is not None:
            self._parent.close()
        if self._client is not None:
            try:
                self._client.cleanup()
            except Exception:
                pass
            self._client = None


def _crop_png(path: str, img_box) -> bytes:
    from PIL import Image

    with Image.open(path) as image:
        crop = image.crop(img_box)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()


def _once(fn, *args):
    def run() -> bool:
        fn(*args)
        return False

    return run
