"""IBus 输入法直接提交（Tier 1）。仅在确认支持 current_input_context 的会话中启用。"""
from __future__ import annotations

import shutil
import subprocess


def probe_ibus_commit_support(bus) -> tuple[bool, str]:
    """探测当前会话是否支持 IBus current_input_context 直提交流程。"""
    if not bus.is_connected():
        return False, "IBus 守护进程未连接"
    gdbus = shutil.which("gdbus")
    if gdbus:
        try:
            result = subprocess.run(
                [
                    gdbus,
                    "introspect",
                    "--session",
                    "--dest",
                    "org.freedesktop.IBus",
                    "--object-path",
                    "/org/freedesktop/IBus",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0:
                if "CurrentInputContext" in result.stdout:
                    return True, "D-Bus 已暴露 CurrentInputContext"
                return False, "D-Bus 未暴露 CurrentInputContext"
        except Exception:
            pass
    try:
        ctx_path = bus.current_input_context()
    except Exception as exc:
        detail = str(exc).splitlines()[0]
        return False, f"当前会话不支持 current_input_context ({detail})"
    if ctx_path:
        return True, f"已检测到输入上下文 {ctx_path}"
    return True, "当前会话支持 current_input_context，将按需尝试直接提交"


class IBusCommitter:
    def __init__(self, bus=None, *, available: bool = False, reason: str = "未初始化") -> None:
        self.bus = bus
        self.available = available
        self.reason = reason

    @classmethod
    def connect(cls) -> "IBusCommitter":
        try:
            import gi

            gi.require_version("IBus", "1.0")
            from gi.repository import IBus as IBusLib

            bus = IBusLib.Bus()
            if not bus.is_connected():
                return cls(None, available=False, reason="IBus 守护进程未运行")
            available, note = probe_ibus_commit_support(bus)
            return cls(bus if available else None, available=available, reason=note)
        except Exception as exc:
            return cls(None, available=False, reason=str(exc))

    def commit(self, text: str) -> bool:
        """通过 IBus 输入法框架直接提交文字到焦点窗口的输入上下文。"""
        if self.bus is None or not self.available:
            return False
        try:
            import gi

            gi.require_version("IBus", "1.0")
            from gi.repository import IBus as IBusLib

            if not self.bus.is_connected():
                return False
            ctx_path = self.bus.current_input_context()
            if not ctx_path:
                return False
            ctx = IBusLib.InputContext.get_input_context(ctx_path, self.bus.get_connection())
            if ctx is None:
                return False
            ctx.commit_text(IBusLib.Text.new_from_string(text))
            return True
        except Exception as exc:
            detail = str(exc).splitlines()[0]
            self.bus = None
            self.available = False
            self.reason = detail
            print(f"  IBus: 直接提交不可用，回退到剪贴板模式 ({detail})", flush=True)
            return False
