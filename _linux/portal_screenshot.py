"""xdg-desktop-portal Screenshot：非交互整屏截图 + 临时父窗口。"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import traceback
import urllib.parse
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11  # noqa: E402
except Exception:
    GdkX11 = None

from _linux.portal_dbus import (  # noqa: E402
    PORTAL_NAME,
    PORTAL_PATH,
    SCREENSHOT_IFACE,
    dbus_module,
    register_portal_host_app,
)


class PortalScreenshotClient:
    """xdg-desktop-portal Screenshot：非交互整屏截图，返回本地 PNG 临时文件。"""

    def __init__(self) -> None:
        self._dbus = dbus_module()
        self._bus = self._dbus.SessionBus()
        obj = self._bus.get_object(PORTAL_NAME, PORTAL_PATH)
        self._iface = self._dbus.Interface(obj, SCREENSHOT_IFACE)
        props = self._dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        version = int(props.Get(SCREENSHOT_IFACE, "version"))
        if version < 1:
            raise RuntimeError(f"Screenshot portal 版本过低: {version}")
        register_portal_host_app(self._bus)
        self._request_matches: list = []
        self._timeout_id = 0
        self._done = False
        self._callback = None
        self._error_cb = None
        self._temp_path: str | None = None

    def capture(self, callback, error_callback, parent_window: str = "") -> None:
        self._callback = callback
        self._error_cb = error_callback
        token = f"ovi_shot_{os.getpid()}_{int(time.time() * 1000) % 100000000}"
        options = self._dbus.Dictionary(
            {
                "handle_token": self._dbus.String(token),
                "interactive": self._dbus.Boolean(False),
                "modal": self._dbus.Boolean(False),
            },
            signature="sv",
        )
        try:
            handle = self._iface.Screenshot(parent_window, options)
        except Exception as exc:
            self._finish_error(f"调用 Screenshot 失败: {exc}")
            return
        try:
            req = self._bus.get_object(PORTAL_NAME, handle)
            match = self._dbus.Interface(
                req, "org.freedesktop.portal.Request"
            ).connect_to_signal("Response", self._on_response)
            self._request_matches.append((req, match))
        except Exception as exc:
            self._finish_error(f"订阅 Screenshot 响应失败: {exc}")
            return
        self._timeout_id = GLib.timeout_add(30000, self._on_timeout)

    def _clear_requests(self) -> None:
        for _req, match in self._request_matches:
            try:
                match.remove()
            except Exception:
                pass
        self._request_matches.clear()
        if self._timeout_id:
            try:
                GLib.source_remove(self._timeout_id)
            except Exception:
                pass
            self._timeout_id = 0

    @staticmethod
    def _uri_to_path(uri: str) -> str | None:
        try:
            parsed = urllib.parse.urlparse(uri)
            if parsed.scheme in ("", "file"):
                path = urllib.parse.unquote(parsed.path or uri)
                return path or None
        except Exception:
            pass
        return None

    def _on_response(self, response, results) -> None:
        if self._done:
            return
        if int(response) != 0:
            self._finish_error("截图被用户取消或门户拒绝")
            return
        uri = results.get("uri") if results else None
        if not uri:
            self._finish_error("portal 未返回截图 URI")
            return
        source = self._uri_to_path(str(uri))
        if not source or not os.path.exists(source):
            self._finish_error("portal 返回的截图文件不存在")
            return
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp_path = f.name
            shutil.copyfile(source, tmp_path)
        except Exception as exc:
            self._finish_error(f"复制截图失败: {exc}")
            return
        # portal 的非交互截图会在 ~/图片 下新建 Screenshot-N.png；
        # 翻译流程只用临时副本，删除源文件避免每次截图都给用户留垃圾。
        try:
            Path(source).unlink(missing_ok=True)
        except Exception:
            pass
        self._temp_path = tmp_path
        self._finish_ok(tmp_path)

    def _finish_ok(self, path: str) -> None:
        if self._done:
            Path(path).unlink(missing_ok=True)
            return
        self._done = True
        self._clear_requests()
        cb = self._callback
        self._callback = None
        self._temp_path = None  # 所有权交给调用方
        if cb:
            try:
                cb(path)
            except Exception:
                traceback.print_exc()
                Path(path).unlink(missing_ok=True)

    def _finish_error(self, message: str) -> None:
        if self._done:
            return
        self._done = True
        self._clear_requests()
        cb = self._error_cb
        self._callback = None
        self._error_cb = None
        if cb:
            try:
                cb(message)
            except Exception:
                traceback.print_exc()

    def _on_timeout(self) -> bool:
        self._finish_error("截图超时（30s）")
        return False

    def cleanup(self) -> None:
        if not self._done:
            self._done = True
            self._clear_requests()
        if self._temp_path:
            Path(self._temp_path).unlink(missing_ok=True)
            self._temp_path = None

class PortalParentWindow:
    """portal 的系统访问对话框只允许焦点应用显示。

    daemon 没有前台窗口，所以临时建一个 2x2 透明窗口并 present()；
    拿到 X11 XID 后作为 parent_window 传给 Screenshot portal。
    """

    def __init__(self) -> None:
        self._win = None

    def close(self) -> None:
        """关闭 portal 截图用的 2x2 焦点窗口。"""
        win = self._win
        self._win = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def open(self, callback) -> None:
        self.close()
        if GdkX11 is None:
            callback("")
            return
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_decorated(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_above(True)
        win.set_accept_focus(True)
        win.set_default_size(2, 2)
        try:
            win.set_opacity(0.0)
        except Exception:
            pass
        win.move(0, 0)
        win.show_all()
        win.present()
        self._win = win

        attempts = 0

        def ready() -> bool:
            nonlocal attempts
            attempts += 1
            if attempts > 60:  # ~1.8s；窗口仍未 map，交给 portal 超时/错误处理
                callback("")
                return False
            try:
                if not win.get_mapped():
                    return True
            except Exception:
                return False
            gdk_win = win.get_window()
            if gdk_win is None:
                return True
            try:
                xid = GdkX11.X11Window.get_xid(gdk_win)
            except Exception:
                callback("")
                return False
            callback(f"x11:0x{xid:x}")
            return False

        GLib.timeout_add(30, ready)
