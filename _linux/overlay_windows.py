"""WebKit 宿主窗口：常驻状态栏（/overlay-status）与截图译文浮层（/overlay-translate）。

页面只来自本机 https 产品服务（`_webkit_host` 证书锁定、禁止 file://）；状态栏不抢焦点，
内容只由页面消费 GET /api/v1/events。译文浮层在选区原位显示，页面通知结果/错误已呈现后 10s / Esc / 窗口外点击 / 任意键关闭，
关闭必释放 pointer grab（旧 daemon.TranslationOverlay 的同一组规则）。
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib  # noqa: E402

from _overlay import (  # noqa: E402
    STATUS_PAGE_PATH,
    STATUS_WINDOW_POLICY,
    TRANSLATE_PAGE_PATH,
    TRANSLATE_PRESENTED_HANDLER,
    TRANSLATE_WINDOW_POLICY,
    translate_generation_script,
    translate_image_script,
)
from _desktop.translate_lifecycle import parse_presented_message  # noqa: E402
from _webkit_host import WebKitStatusHost  # noqa: E402

STATUS_SIZE = (360, 96)
TRANSLATE_MIN_SIZE = (320, 140)


STATUS_RELOAD_MS = 2000


class StatusWindow:
    def __init__(self, *, origin: str, cert: Path) -> None:
        self.host = WebKitStatusHost(cert=cert, policy=STATUS_WINDOW_POLICY)
        self._url = origin + STATUS_PAGE_PATH
        self._retry_id = 0
        self.window = self.host.create_status_window(self._url)
        # 服务端可能晚于 daemon 就绪：加载失败（连接拒绝等）2s 后重新 navigate；TLS 校验不放宽。
        self.host.webview.connect("load-failed", self._on_load_failed)
        self.window.set_default_size(*STATUS_SIZE)
        self.window.show_all()
        GLib.idle_add(self._position_bottom_left)

    def _position_bottom_left(self) -> bool:
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        alloc = self.window.get_allocation()
        self.window.move(geo.x + 20, geo.y + geo.height - alloc.height - 50)
        return False

    def _on_load_failed(self, _webview, _event, _uri, _error) -> bool:
        if not self._retry_id:
            self._retry_id = GLib.timeout_add(STATUS_RELOAD_MS, self._reload)
        return False

    def _reload(self) -> bool:
        self._retry_id = 0
        if self.host.webview is not None:
            self.host.load(self._url)
        return False

    def set_visible(self, visible: bool) -> None:
        if visible:
            self.window.show_all()
        else:
            self.window.hide()

    def destroy(self) -> None:
        self.host.destroy()


class TranslateWindow:
    """译文浮层窗口。生命周期（何时自动关）由 `_desktop.translate_lifecycle` 决定；本类只负责：
    加载页面、接收本窗口私有桥上的“已呈现”通知并校验（URI / generation / 白名单）、Esc/窗外点击关闭、
    pointer grab 与释放。不从网络请求完成推断渲染；没有固定存活上限计时器。
    """

    def __init__(
        self,
        *,
        origin: str,
        cert: Path,
        png: bytes,
        rect: tuple[int, int, int, int],
        events,
    ) -> None:
        self._events = events
        self._closed = False
        self._pointer_grabbed = False
        self._timers: list[int] = []
        self._url = origin + TRANSLATE_PAGE_PATH
        self._generation = secrets.token_urlsafe(24)
        self.host = WebKitStatusHost(cert=cert, policy=TRANSLATE_WINDOW_POLICY, origin=origin)
        # 桥与 generation 都在 load 之前就绪：页面最早期的接口说明/配置失败也能通知到。
        win = self.host.create_status_window(
            self._url,
            user_script=translate_generation_script(self._generation) + translate_image_script(png),
            message_handler=(TRANSLATE_PRESENTED_HANDLER, self._on_page_message),
        )
        self.window = win
        x, y, w, h = rect
        win.set_resizable(False)
        win.set_default_size(max(w, TRANSLATE_MIN_SIZE[0]), max(h, TRANSLATE_MIN_SIZE[1]))
        win.move(x, y)
        win.connect("key-press-event", self._on_key_press)
        win.connect("grab-broken-event", self._on_grab_broken)
        win.connect("destroy", self._on_destroy)
        win.show_all()
        win.present()  # 获取焦点以接收 Esc
        # idle 阶段窗口可能尚未 viewable，grab 会返回 NOT_VIEWABLE；延后一拍。
        self._timers.append(GLib.timeout_add(80, self._grab_pointer))
        # 轮询检测窗口外点击（grab 在 GTK3 下不一定触发 button-press-event）
        self._timers.append(GLib.timeout_add(100, self._check_outside_click))

    # ---- 页面“已呈现”通知 ----------------------------------------------------
    def _on_page_message(self, raw: str | None) -> None:
        if self._closed or self.host.webview is None:
            return
        kind = parse_presented_message(
            raw, generation=self._generation, page_uri=self.host.webview.get_uri(), expected_url=self._url
        )
        if kind is not None:
            self._events.presented(kind)

    def close(self) -> bool:
        if not self._closed:
            try:
                self.window.destroy()
            except Exception:
                self._on_destroy()
        return False

    def _on_key_press(self, widget, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _grab_pointer(self) -> bool:
        """抓取 pointer（owner_events=True，页面内点击照常），把窗口外 ButtonPress 也路由到本窗口。"""
        gdk_win = self.window.get_window()
        if gdk_win is None:
            return False
        seat = gdk_win.get_display().get_default_seat()
        if seat is None:
            return False
        status = seat.grab(gdk_win, Gdk.SeatCapabilities.ALL_POINTING, True, None, None, None)
        self._pointer_grabbed = status == Gdk.GrabStatus.SUCCESS
        if not self._pointer_grabbed:
            print(f"  TranslateWindow: pointer grab failed ({int(status)})", file=sys.stderr)
        return False

    def _on_grab_broken(self, widget, event) -> bool:
        self._pointer_grabbed = False
        return False

    def _check_outside_click(self) -> bool:
        """指针在窗口外且有鼠标按键按下时关闭（不依赖焦点状态）。"""
        gdk_win = self.window.get_window()
        if gdk_win is None or self._closed:
            return False
        pointer = Gdk.Display.get_default().get_default_seat().get_pointer()
        if pointer is None:
            return True
        _, rpx, rpy = pointer.get_position()
        wx, wy = self.window.get_position()
        ww, wh = self.window.get_size()
        if not (wx <= rpx <= wx + ww and wy <= rpy <= wy + wh):
            _, _, _, mask = gdk_win.get_device_position(pointer)
            buttons = (
                Gdk.ModifierType.BUTTON1_MASK
                | Gdk.ModifierType.BUTTON2_MASK
                | Gdk.ModifierType.BUTTON3_MASK
            )
            if mask & buttons:
                self.close()
                return False
        return True

    def _on_destroy(self, *args) -> None:
        if self._closed:
            return
        self._closed = True
        # 最优先释放 pointer grab（鼠标被圈住的唯一来源），无论后续是否异常都通知关闭。
        try:
            if self._pointer_grabbed:
                display = Gdk.Display.get_default()
                seat = display.get_default_seat() if display is not None else None
                if seat is not None:
                    seat.ungrab()
        except Exception:
            pass
        finally:
            self._pointer_grabbed = False
            try:
                self.host.unregister_message_handler()  # 注销桥：晚到的页面消息到不了宿主
            except Exception:
                pass
            for sid in self._timers:
                try:
                    GLib.source_remove(sid)
                except Exception:
                    pass
            self._timers.clear()
            self._events.closed()
