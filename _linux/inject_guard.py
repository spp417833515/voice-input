"""注入互斥与回流保护窗口。

lock：串行化所有按键注入与修饰键清理（panic/watchdog）。杜绝转写线程 / 重复输入线程 / 看门狗
三方并发写同一 uinput / XTest 造成的"撕键"：一方按下 Ctrl 还没发 V 时被另一方 release，
粘贴退化成裸 v，或 X 层修饰键卡死。用 RLock：注入持锁期间其 finally 仍会清修饰键（同线程重入）。

until：此刻之前抓取器忽略按键事件，防止自己注入的 Ctrl/Shift/V 经 XInput2 raw / evdev 回流后
再次匹配热键 → 触发录音/重复输入的自反馈回环。
"""
from __future__ import annotations

import threading
import time


class InjectGuard:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.until = 0.0

    def open_window(self, seconds: float) -> None:
        self.until = time.monotonic() + seconds

    def active(self) -> bool:
        return time.monotonic() < self.until
