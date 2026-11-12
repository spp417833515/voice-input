"""空闲兜底看门狗：周期性断言无残留修饰键 / 指针 grab，并向 systemd 喂狗。

修复两类偶发故障并使其在 ~2s 内自愈：
- 注入中途异常 → Ctrl/Super 卡在按下态 → 经 Synergy 注入的按键全变快捷键；
- 浮窗 grab 未释放 → 鼠标被圈住。
录音中或浮窗活跃时跳过（浮窗自己持有 grab，不可越权释放）；
但录音若超过 voice_max_s 视为 release 丢失导致的卡死，强制 finish（不是 cancel），
防止 voice_active 永久为 True 把整个自愈系统旁路。

每轮向 systemd 喂狗（WATCHDOG=1）：本回调不再被调度 = GTK 主循环卡死，
systemd 在 WatchdogSec 后 SIGABRT 重启 —— 这是"进程活着但主循环挂死"唯一兜底。
"""
from __future__ import annotations

import time
from collections.abc import Callable

from _linux.inject_guard import InjectGuard
from _linux.modifiers import ModifierJanitor

VOICE_MAX_SECONDS = 120.0


class IdleSafetyWatchdog:
    def __init__(
        self,
        *,
        manager,
        janitor: ModifierJanitor,
        guard: InjectGuard,
        overlay_active: Callable[[], bool],
        notify: Callable[[str], object],
        voice_max_s: float = VOICE_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.manager = manager
        self.janitor = janitor
        self.guard = guard
        self.overlay_active = overlay_active
        self.notify = notify
        self.voice_max_s = voice_max_s
        self.clock = clock

    def tick(self) -> bool:
        """GLib.timeout_add 回调；始终返回 True 以保持调度。"""
        self.notify("WATCHDOG=1")
        grabber = self.manager
        if grabber is not None and getattr(grabber, "voice_active", False):
            started = getattr(grabber, "voice_started_at", 0.0)
            if not (started and (self.clock() - started) > self.voice_max_s):
                return True  # 正常录音中，触发键/修饰键可能正被按住，跳过
            print(f"[watchdog] voice_active 超过 {self.voice_max_s:.0f}s, 判定 "
                  "release 丢失, 强制 finish", flush=True)
            try:
                grabber.force_stop_voice()
            except Exception:
                pass
            # 落到下方执行 panic 清理
        if self.overlay_active():
            return True  # 浮窗活跃，其 pointer grab 由浮窗自身生命周期管理
        # 注入进行中（持有注入锁）则本轮跳过，绝不撕正在进行的粘贴
        if self.guard.lock.acquire(blocking=False):
            try:
                # uinput 层 + 指针 grab 无条件清理（对用户真实按键无副作用）
                self.janitor.clear_uinput()
                self.janitor.release_seat_grab()
                # XTest 层是唯一可能"误伤用户正常长按"的清理，故智能化：
                # 有卡键检测能力(XInput2)时仅在确有卡住的修饰键才清；无检测能力保持原兜底。
                if grabber is not None and hasattr(grabber, "has_stuck_modifiers"):
                    if grabber.has_stuck_modifiers():
                        print("[watchdog] 检测到卡住的修饰键, 清理 XTest 层", flush=True)
                        self.janitor.clear_xtest()
                else:
                    self.janitor.clear_xtest()
            finally:
                self.guard.lock.release()
        return True
