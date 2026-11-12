"""快捷键后端共享的动作层：录音开始/停止、截图、重复输入、关闭浮层。

后端只负责把"按键按下/松开"翻译成这里的调用，不复制业务逻辑；业务实现由 `_desktop` 注入。
portal 路径已经有原生 Deactivated，无需补发修饰键释放。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from gi.repository import GLib

from _hotkey import HotkeyBindings
from _linux.inject_guard import InjectGuard
from _linux.modifiers import ModifierJanitor


class HotkeyActions(Protocol):
    def voice_start(self) -> bool: ...

    def voice_stop(self) -> None: ...

    def screenshot(self) -> None: ...

    def repeat(self) -> None: ...

    def overlay_active(self) -> bool: ...

    def close_overlay(self) -> bool: ...


@dataclass
class HotkeyContext:
    actions: HotkeyActions
    bindings: HotkeyBindings
    janitor: ModifierJanitor
    guard: InjectGuard
    session_type: str


class HotkeyActionMixin:
    ctx: HotkeyContext
    voice_active: bool
    voice_started_at: float
    _pending_release_id: int
    _release_modifiers_on_stop = True

    def _init_hotkey_actions(self, ctx: HotkeyContext) -> None:
        self.ctx = ctx
        self.voice_active = False
        self.voice_started_at = 0.0  # 录音开始时刻，供看门狗判定 release 丢失卡死
        self._pending_release_id = 0

    def _cancel_pending_release(self) -> None:
        if self._pending_release_id:
            try:
                GLib.source_remove(self._pending_release_id)
            except Exception:
                pass
            self._pending_release_id = 0

    def _on_voice_press(self) -> None:
        self._cancel_pending_release()
        if not self.voice_active:
            self.voice_active = True
            self.voice_started_at = time.monotonic()
            if not self.ctx.actions.voice_start():
                self.voice_active = False

    def _schedule_release(self) -> bool:
        """50ms 延迟释放：X11 自动重复会在 KeyRelease 后立刻补 KeyPress。"""
        self._cancel_pending_release()
        self._pending_release_id = GLib.timeout_add(50, self._do_release)
        return False

    def _do_release(self) -> bool:
        self._pending_release_id = 0
        if self.voice_active:
            self.voice_active = False
            self.ctx.actions.voice_stop()
            if self._release_modifiers_on_stop:
                self.ctx.janitor.release_hotkey(self.ctx.bindings.voice)
        return False

    def _on_screenshot(self) -> bool:
        self.ctx.actions.screenshot()
        return False

    def _on_repeat(self) -> bool:
        self.ctx.actions.repeat()
        return False

    def _close_overlay_on_key(self) -> None:
        """任意按键 (含修饰键单独按下) → 关活跃浮窗, 不依赖焦点。"""
        if self.ctx.actions.overlay_active():
            GLib.idle_add(self.ctx.actions.close_overlay)
