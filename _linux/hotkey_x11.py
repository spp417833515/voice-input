"""X11 XGrabKey 全局快捷键（X11 独占），通过 GLib.io_add_watch 集成到 GTK 事件循环。"""
from __future__ import annotations

import sys

from gi.repository import GLib
from Xlib import X
from Xlib import display as xdisplay

from _linux.hotkey_actions import HotkeyActionMixin, HotkeyContext
from _linux.x11keys import LOCK_MASKS, parse_hotkey


class HotkeyGrabber(HotkeyActionMixin):
    """XGrabKey 快捷键管理，通过 GLib.io_add_watch 集成到 GTK 事件循环。

    自动重复检测使用 GLib.timeout_add 延迟释放方案：
    - KeyRelease 时不立即执行，而是设置 50ms 定时器
    - 如果 50ms 内收到同键 KeyPress，说明是自动重复，取消定时器
    - 如果 50ms 后无 KeyPress，说明是真正释放，执行 stop
    """

    def __init__(self, ctx: HotkeyContext) -> None:
        self._init_hotkey_actions(ctx)
        self.dpy = xdisplay.Display()
        self._grab_errors: list[str] = []
        self._reported_grab_error = False
        self.dpy.set_error_handler(self._on_x_error)
        self.root = self.dpy.screen().root

        # 从配置解析快捷键
        v_mask, v_sym = parse_hotkey(self.ctx.bindings.voice)
        s_mask, s_sym = parse_hotkey(self.ctx.bindings.screenshot)
        r_mask, r_sym = parse_hotkey(self.ctx.bindings.repeat)

        self.voice_kc = self.dpy.keysym_to_keycode(v_sym)
        self.voice_mask = v_mask
        self.screen_kc = self.dpy.keysym_to_keycode(s_sym)
        self.screen_mask = s_mask
        self.repeat_kc = self.dpy.keysym_to_keycode(r_sym)
        self.repeat_mask = r_mask

    def _on_x_error(self, err, *args):
        """收集 XGrabKey 的 BadAccess；不能像旧代码那样静默吞掉冲突。"""
        try:
            self._grab_errors.append(str(err))
        except Exception:
            pass

    def grab(self) -> None:
        for lock in LOCK_MASKS:
            self.root.grab_key(
                self.voice_kc,
                self.voice_mask | lock,
                True,
                X.GrabModeAsync,
                X.GrabModeAsync,
            )
            self.root.grab_key(
                self.screen_kc,
                self.screen_mask | lock,
                True,
                X.GrabModeAsync,
                X.GrabModeAsync,
            )
            self.root.grab_key(
                self.repeat_kc,
                self.repeat_mask | lock,
                True,
                X.GrabModeAsync,
                X.GrabModeAsync,
            )
        self.dpy.flush()
        # 同步一次，让 BadAccess 立即进入错误处理器；XGrabKey 是异步的。
        try:
            self.dpy.sync()
        except Exception:
            pass
        if self._grab_errors:
            if not self._reported_grab_error:
                self._reported_grab_error = True
                for error in self._grab_errors:
                    print(f"  快捷键: XGrabKey 冲突/错误: {error}", flush=True)
            self._grab_errors.clear()

    def ungrab(self) -> None:
        for lock in LOCK_MASKS:
            self.root.ungrab_key(self.voice_kc, self.voice_mask | lock)
            self.root.ungrab_key(self.screen_kc, self.screen_mask | lock)
            self.root.ungrab_key(self.repeat_kc, self.repeat_mask | lock)
        self.dpy.flush()

    def start(self) -> None:
        self.grab()
        GLib.io_add_watch(self.dpy.fileno(), GLib.IO_IN, self._on_xlib_event)
        GLib.timeout_add(500, self._refresh_grab)

    def _refresh_grab(self) -> bool:
        if not self.voice_active:
            try:
                self.grab()  # XGrabKey 对同一客户端幂等，不做 ungrab 避免注册空白
            except Exception:
                pass
        return True

    def _handle_event(self, event) -> None:
        if event.type == X.KeyPress:
            if event.detail == self.voice_kc:
                self._on_voice_press()
            elif event.detail == self.screen_kc:
                self._on_screenshot()
            elif event.detail == self.repeat_kc:
                self._on_repeat()
        elif event.type == X.KeyRelease:
            if event.detail == self.voice_kc and self.voice_active:
                self._schedule_release()

    def _on_xlib_event(self, fd, condition) -> bool:
        try:
            self.dpy.sync()
        except Exception as e:
            print(f"XGrabKey sync error: {e}", file=sys.stderr)
            return True
        while self.dpy.pending_events() > 0:
            try:
                event = self.dpy.next_event()
                self._handle_event(event)
            except Exception as e:
                print(f"XGrabKey event error: {e}", file=sys.stderr)
        return True

    def cleanup(self) -> None:
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
