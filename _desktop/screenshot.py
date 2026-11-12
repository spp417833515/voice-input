"""截图翻译：平台截图/选区 → 在选区原位打开译文浮层页面（页面自己调 /api/v1/ocr-translate）。

浮层生命周期见 `_desktop/translate_lifecycle.py`：结果/错误可见后 10s 关闭、等待超限显式
timeout 错误；Esc / 窗外点击 / 任意键随时关闭。失败与取消都经产品事件流可见，无假译文。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from _contracts import ErrorCode
from _desktop.status import PlatformStatus
from _desktop.translate_lifecycle import Scheduler, TranslateLifecycle
from _linux.screenshot_types import CaptureCallbacks, Rect


class Capture(Protocol):
    def start(self, cb: CaptureCallbacks) -> None: ...

    def active(self) -> bool: ...


class OverlayHandle(Protocol):
    def close(self) -> bool: ...


class OverlayEvents:
    """窗口层回调本浮层的事件（GTK 主线程）。只转给本浮层自己的生命周期，旧浮层的迟到事件不影响新浮层。"""

    def __init__(self, flow: "ScreenshotFlow", lifecycle: TranslateLifecycle) -> None:
        self._flow = flow
        self.lifecycle = lifecycle

    def presented(self, kind: str) -> None:
        """页面已把结果/可见错误画进 DOM（窗口层已校验来源、generation 与白名单）。"""
        self.lifecycle.presented(kind)

    def closed(self) -> None:
        self.lifecycle.window_closed()
        self._flow._closed(self)


OpenOverlay = Callable[[bytes, Rect, OverlayEvents], OverlayHandle]


class ScreenshotFlow:
    def __init__(
        self,
        *,
        capture: Capture,
        open_overlay: OpenOverlay,
        status: PlatformStatus,
        scheduler: Scheduler,
    ) -> None:
        self.capture = capture
        self.open_overlay = open_overlay
        self.status = status
        self.scheduler = scheduler
        self._overlay: OverlayHandle | None = None
        self._events: OverlayEvents | None = None

    def trigger(self) -> None:
        self.close_overlay()
        self.capture.start(
            CaptureCallbacks(
                on_stage=lambda _stage: self.status.busy(),
                on_captured=self._show,
                on_cancelled=self.status.idle,
                on_failed=lambda message: self.status.error(ErrorCode.dependency_failed, message),
            )
        )

    def _show(self, png: bytes, rect: Rect) -> None:
        self.status.idle()  # 截图阶段结束；之后的 busy/idle/error 由浮层页面对自己的请求上报
        self.close_overlay()
        holder: dict[str, OverlayHandle | None] = {"handle": None}

        def close_window() -> None:
            handle = holder["handle"]
            if handle is not None:
                handle.close()

        lifecycle = TranslateLifecycle(scheduler=self.scheduler, status=self.status, close_window=close_window)
        events = OverlayEvents(self, lifecycle)
        try:
            handle = self.open_overlay(png, rect, events)
        except Exception as exc:
            lifecycle.window_closed()
            self.status.error(ErrorCode.dependency_failed, f"译文浮层无法打开：{str(exc)[:80]}")
            return
        holder["handle"] = handle
        if lifecycle.state == "closed":
            return  # 打开过程中已被关闭
        self._overlay = handle
        self._events = events

    def _closed(self, events: OverlayEvents) -> None:
        if self._events is events:
            self._overlay = None
            self._events = None

    @property
    def lifecycle(self) -> TranslateLifecycle | None:
        return self._events.lifecycle if self._events is not None else None

    def overlay_active(self) -> bool:
        return self._overlay is not None

    def close_overlay(self) -> bool:
        """Esc / 任意键 / 新截图：关闭当前浮层（GLib.idle_add 回调，返回 False）。"""
        events = self._events
        self._overlay = None
        self._events = None
        if events is not None:
            events.lifecycle.close("user")
        return False
