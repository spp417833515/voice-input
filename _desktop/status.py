"""平台状态 → 产品事件流（/api/v1/events/platform）。后台串行发送，绝不阻塞 GTK 主线程。

状态栏页面只消费 GET /api/v1/events；平台本地才知道的状态（开麦失败、截图中、截图失败、
没有可重复的记录）经这里进入同一事件流，不在宿主另画一套状态。
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from _contracts import ErrorCode


class PlatformStatus:
    def __init__(self, post: Callable[[dict], None], *, start_thread: bool = True) -> None:
        self._post = post
        self._queue: queue.Queue[dict | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(target=self._run, name="platform-status", daemon=True)
            self._thread.start()

    def busy(self) -> None:
        self._queue.put({"event": "busy"})

    def idle(self) -> None:
        self._queue.put({"event": "idle"})

    def error(self, code: ErrorCode, message: str) -> None:
        self._queue.put({"event": "error", "error": code.value, "message": message})

    def drain(self) -> None:
        """测试/退出用：同步发送已排队的事件。"""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                self._send(item)

    def close(self) -> None:
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._send(item)

    def _send(self, item: dict) -> None:
        try:
            self._post(item)
        except Exception as exc:
            # 服务不可达时状态无处可显示，只能记日志；业务结果仍以 job 状态为准。
            print(f"  状态: 上报失败 {item.get('event')}: {exc}", flush=True)
