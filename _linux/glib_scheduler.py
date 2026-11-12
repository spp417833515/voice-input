"""GLib 主循环上的一次性计时器（实现 `_desktop.translate_lifecycle.Scheduler`）。"""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import GLib


class GLibScheduler:
    def __init__(self) -> None:
        self._live: set[int] = set()

    def schedule(self, delay_ms: int, fn: Callable[[], None]) -> int:
        holder: dict[str, int] = {}

        def fire() -> bool:
            self._live.discard(holder["id"])
            fn()
            return False  # 一次性

        sid = GLib.timeout_add(int(delay_ms), fire)
        holder["id"] = sid
        self._live.add(sid)
        return sid

    def cancel(self, handle: int) -> None:
        # 已触发的 source 已被移除；只移除仍在等待的，避免 GLib 对失效 id 告警
        if handle in self._live:
            self._live.discard(handle)
            GLib.source_remove(handle)
