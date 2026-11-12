"""按会话选择按键所有权后端；portal 负责动作，观察器补旧 XTEST/活动。

Wayland：portal GlobalShortcuts 独占；失败才回退 evdev/XI2 观察器并明确告警"按键未独占"。
X11：XGrabKey 独占；失败回退观察器。
"""
from __future__ import annotations

from _linux.hotkey_actions import HotkeyContext
from _linux.hotkey_evdev import EvdevHotkeyGrabber
from _linux.hotkey_portal import PortalGlobalShortcutsGrabber
from _linux.hotkey_x11 import HotkeyGrabber
from _linux.hotkey_xinput import XInputHotkeyGrabber


class HotkeyManager:
    """按会话选择按键所有权后端；portal 负责动作，观察器补旧 XTEST/活动。"""

    def __init__(self, ctx: HotkeyContext) -> None:
        self.ctx = ctx
        self.active = None          # portal / X11 主后端；回退时指向主观察器
        self.observers: list = []   # Evdev / XInput 被动观察器
        self._portal = None
        self._closed = False
        self.backend_name = "none"

    def _all_objects(self) -> list:
        seen: set[int] = set()
        out: list = []
        for obj in [self.active, self._portal, *self.observers]:
            if obj is None or id(obj) in seen:
                continue
            seen.add(id(obj))
            out.append(obj)
        return out

    @property
    def voice_active(self) -> bool:
        return any(
            getattr(obj, "voice_active", False) for obj in self._all_objects()
        )

    @property
    def voice_started_at(self) -> float:
        for obj in self._all_objects():
            if getattr(obj, "voice_active", False):
                return float(getattr(obj, "voice_started_at", 0.0))
        return 0.0

    def has_stuck_modifiers(self) -> bool:
        for obj in self._all_objects():
            if hasattr(obj, "has_stuck_modifiers"):
                try:
                    return bool(obj.has_stuck_modifiers())
                except Exception:
                    pass
        # 没有卡键检测能力时：X11 保持旧的“无条件清 XTest”兜底；
        # Wayland portal 的修饰键归 compositor，盲目清 XTest 反而会干扰用户。
        return self.ctx.session_type != "wayland"

    def force_stop_voice(self) -> None:
        for obj in self._all_objects():
            if getattr(obj, "voice_active", False):
                try:
                    obj.voice_active = False
                except Exception:
                    pass
        try:
            self.ctx.actions.voice_stop()  # 看门狗到时 = finish，不是 cancel
        except Exception:
            pass
        # 强制复位是最后兜底；此时释放一次修饰键比卡住更安全。
        self.ctx.janitor.release_hotkey(self.ctx.bindings.voice)

    def start(self) -> None:
        if self.ctx.session_type == "wayland":
            self._start_wayland()
        else:
            self._start_x11()

    def _start_observers(self) -> bool:
        self.observers = []
        for cls in (EvdevHotkeyGrabber, XInputHotkeyGrabber):
            try:
                obj = cls(self.ctx)
                obj.actions_enabled = False
                if isinstance(obj, XInputHotkeyGrabber):
                    obj.only_xtest_sources = True
                obj.start()
                self.observers.append(obj)
                print(f"  快捷键: 观察器 {cls.__name__} 已启动", flush=True)
            except Exception as exc:
                print(f"  快捷键: 观察器 {cls.__name__} 不可用 ({exc})", flush=True)
        return bool(self.observers)

    def _start_wayland(self) -> None:
        if PortalGlobalShortcutsGrabber.available():
            self._start_observers()
            try:
                self._portal = PortalGlobalShortcutsGrabber(
                    self.ctx,
                    on_bound=self._on_portal_bound,
                    on_failed=self._on_portal_failed,
                )
                print(
                    "  快捷键: 启用 portal GlobalShortcuts — compositor 独占",
                    flush=True,
                )
                self._portal.start()
                return
            except Exception as exc:
                print(f"  快捷键: portal 初始化失败 ({exc})", flush=True)
                self._portal = None
        self._activate_fallback()

    def _on_portal_bound(self) -> None:
        self.active = self._portal
        self.backend_name = "portal"
        for obj in self.observers:
            # portal 覆盖 compositor 级按键；观察器只保留活动/卡键检测，
            # 不再触发动作，避免 XWayland 源合并导致的潜在双触发。
            obj.actions_enabled = False
        print("  快捷键: portal 已接管；观察器仅做键盘活动", flush=True)

    def _on_portal_failed(self) -> None:
        if self._portal is not None:
            try:
                self._portal.cleanup()
            except Exception:
                pass
        if self.active is self._portal:
            self.active = None
        self._activate_fallback()

    def _activate_fallback(self) -> None:
        if self.active is not None:
            return
        if not self.observers:
            self._start_observers()
        if not self.observers:
            raise RuntimeError("所有快捷键抓取方式均不可用")
        has_evdev = any(
            isinstance(obj, EvdevHotkeyGrabber) for obj in self.observers
        )
        for obj in self.observers:
            obj.actions_enabled = True
            if isinstance(obj, XInputHotkeyGrabber):
                # 有 evdev 时，XInput 只负责 XTEST，避免物理键双触发；
                # 只有 XInput 时，它必须接管所有它能看到的按键。
                obj.only_xtest_sources = has_evdev
        self.active = next(
            (obj for obj in self.observers if isinstance(obj, EvdevHotkeyGrabber)),
            self.observers[0],
        )
        self.backend_name = "+".join(
            "evdev" if isinstance(obj, EvdevHotkeyGrabber) else "xinput"
            for obj in self.observers
        )
        print(
            "  快捷键: 启用观察器回退（按键未独占，可能仍会传给应用）",
            flush=True,
        )

    def _start_x11(self) -> None:
        try:
            self.active = HotkeyGrabber(self.ctx)
            self.backend_name = "XGrabKey"
            print("  快捷键: 启用 XGrabKey — X11 独占", flush=True)
            self.active.start()
        except Exception as exc:
            print(f"  快捷键: XGrabKey 不可用 ({exc})，尝试回退", flush=True)
            self.active = None
            self._activate_fallback()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        seen: set[int] = set()
        for obj in [self._portal, *self.observers, self.active]:
            if obj is None or id(obj) in seen:
                continue
            seen.add(id(obj))
            try:
                obj.cleanup()
            except Exception:
                pass
