"""截图译文浮层的生命周期（纯逻辑，不导入 GTK；计时器经注入的 Scheduler，测试用假时钟）。

沿用旧 daemon.TranslationOverlay 语义：
- 结果显示后 10s 自动关闭（RESULT_READ_MS）；用户 Esc / 窗外点击 / 任意键随时关闭（由窗口与热键层调用 close）。
- 旧版 30s 是“加载/结果卡死时也必须销毁、释放 pointer grab”的防挂上限。这里只约束等待结果阶段
  （PENDING_LIMIT_MS）：到时仍无结果 → 显式 timeout 错误（状态栏可见）并进入同样的 10s 可读期，
  不再吞掉结果到达后的阅读时间。
- 每个浮层只认第一次结果：重复结果/错误不续时；关闭后迟到的结果被忽略，不会重新打开。

“结果已呈现”的唯一来源是浮层页面在真实 DOM 更新之后经本窗口私有 WebKit script-message 桥发出的
一次通知（translate-controller.js createPresentedNotifier → _linux/overlay_windows.TranslateWindow →
parse_presented_message），不从网络请求完成去猜渲染；不新增 HTTP 字段或全局事件。

状态栏单源：页面 translate-controller 对自己的请求经 POST /api/v1/events/platform 报
busy / idle / error（带真实错误码，且不阻塞 DOM），宿主不重复报这些；宿主只补页面做不到的两件事：
等待超限的 timeout 错误，以及用户在等待中关掉浮层（页面已销毁，来不及报 idle）时撤掉“处理中”。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from _contracts import ErrorCode

RESULT_READ_MS = 10_000
PENDING_LIMIT_MS = 30_000


class Scheduler(Protocol):
    def schedule(self, delay_ms: int, fn: Callable[[], None]) -> Any: ...

    def cancel(self, handle: Any) -> None: ...


class Status(Protocol):
    def idle(self) -> None: ...

    def error(self, code: ErrorCode, message: str) -> None: ...


class TranslateLifecycle:
    """状态：pending → shown(result|error) → closed；或任意状态 → closed（用户关闭）。"""

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        status: Status,
        close_window: Callable[[], None],
        read_ms: int = RESULT_READ_MS,
        pending_limit_ms: int = PENDING_LIMIT_MS,
    ) -> None:
        self._scheduler = scheduler
        self._status = status
        self._close_window = close_window
        self._read_ms = read_ms
        self._pending_limit_ms = pending_limit_ms
        self.state = "pending"
        self.outcome: str | None = None  # "result" / "error" / "timeout"
        self.closed_by: str | None = None
        self.error: tuple[ErrorCode, str] | None = None
        self._read_handle: Any = None
        self._pending_handle = scheduler.schedule(pending_limit_ms, self._pending_expired)

    # ---- 页面“已呈现”通知（窗口层回调，GTK 主线程） --------------------------
    def presented(self, kind: str) -> None:
        """页面已把结果（kind="result"）或可见错误（kind="error"）画进 DOM。只认第一次。"""
        if self.state != "pending" or kind not in PRESENTED_KINDS:
            return  # 重复不续时；关闭后迟到不重开；未知种类忽略
        self._settle(kind)

    def _pending_expired(self) -> None:
        self._pending_handle = None
        if self.state != "pending":
            return
        self._settle("timeout")
        self.error = (ErrorCode.timeout, "pending_limit")
        self._status.error(
            ErrorCode.timeout,
            f"截图翻译 {self._pending_limit_ms // 1000}s 内未返回结果，已放弃等待（Esc/点击关闭）",
        )

    def _settle(self, outcome: str) -> None:
        self.state = "shown"
        self.outcome = outcome
        if self._pending_handle is not None:
            self._scheduler.cancel(self._pending_handle)
            self._pending_handle = None
        self._read_handle = self._scheduler.schedule(self._read_ms, self._read_expired)

    def _read_expired(self) -> None:
        self._read_handle = None
        self.close("auto")

    # ---- 关闭（用户 Esc / 窗外点击 / 任意键 / 自动） -------------------------
    def close(self, reason: str = "user") -> None:
        if self.state == "closed":
            return
        was_pending = self.state == "pending"
        self.state = "closed"
        self.closed_by = reason
        for attr in ("_pending_handle", "_read_handle"):
            handle = getattr(self, attr)
            if handle is not None:
                self._scheduler.cancel(handle)
                setattr(self, attr, None)
        if was_pending:
            self._status.idle()  # 用户放弃等待：撤掉“处理中”
        self._close_window()

    def window_closed(self) -> None:
        """窗口已被销毁（任何原因）时调用，保证计时器清理；不再回调 close_window。"""
        if self.state == "closed":
            return
        self._close_window, close_window = (lambda: None), self._close_window
        try:
            self.close("window")
        finally:
            self._close_window = close_window


# ---- 页面通知校验（纯函数；窗口层只把通过校验的种类交给生命周期） -------------------
PRESENTED_KINDS = frozenset({"result", "error"})
PRESENTED_MAX_BYTES = 256


def parse_presented_message(raw: str | None, *, generation: str, page_uri: str | None, expected_url: str) -> str | None:
    """严格白名单：{"v":1,"generation":<本窗口>,"state":"result"|"error"}，且来自本窗口当前的译文页 URI。"""
    import hmac
    import json
    from urllib.parse import urlsplit

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > PRESENTED_MAX_BYTES or not page_uri:
        return None
    cur, want = urlsplit(page_uri), urlsplit(expected_url)
    if (cur.scheme, cur.netloc, cur.path) != (want.scheme, want.netloc, want.path):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {"v", "generation", "state"}:
        return None
    if data["v"] != 1 or type(data["v"]) is not int:
        return None
    gen = data["generation"]
    if not isinstance(gen, str) or not hmac.compare_digest(gen.encode(), generation.encode()):
        return None
    state = data["state"]
    return state if state in PRESENTED_KINDS else None
