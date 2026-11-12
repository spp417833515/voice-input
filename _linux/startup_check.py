"""守护进程启动自检：只读检查本机工具、portal、快捷键冲突，结果打印到日志。

模型/服务就绪状态归设置页 `/api/setup/check`，这里不重复探测。
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping

from _hotkey import HotkeyBindings
from _linux.conflicts import ConflictScan, describe_scan, scan_hotkey_conflicts
from _linux.portal_dbus import SCREENSHOT_IFACE


def startup_issues(
    bindings: HotkeyBindings,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    portal_version: Callable[[str], int | None],
    screenshot_tool: str | None,
    conflicts: Callable[[HotkeyBindings], ConflictScan] = scan_hotkey_conflicts,
) -> list[str]:
    env = os.environ if environ is None else environ
    issues: list[str] = []
    session = env.get("XDG_SESSION_TYPE", "x11")
    gdk_backend = env.get("GDK_BACKEND", "auto")
    if session == "wayland":
        if not env.get("DISPLAY"):
            issues.append(
                "Wayland 环境下未检测到 XWayland（$DISPLAY 未设置），"
                "快捷键和窗口拖拽可能不可用。请安装 xwayland"
            )
        elif gdk_backend not in ("x11",):
            issues.append(
                f"Wayland 环境但 GDK_BACKEND={gdk_backend}，"
                "拖拽/快捷键可能异常。建议设置 GDK_BACKEND=x11"
            )
        if not which("xclip") and not which("xsel"):
            issues.append("Wayland 环境缺少 xclip，请安装: sudo apt install xclip")
        if portal_version(SCREENSHOT_IFACE) is None:
            issues.append(
                "Wayland 截图需要 xdg-desktop-portal（GNOME）：未检测到 "
                "org.freedesktop.portal.Screenshot"
            )
    else:
        if not which("xclip") and not which("xsel"):
            issues.append("缺少剪贴板工具: sudo apt install xclip")
        if not which("xdotool"):
            issues.append("建议安装 xdotool 以提高快捷键可靠性: sudo apt install xdotool")
        if not screenshot_tool:
            issues.append("缺少截图工具: sudo apt install maim")
    issues.extend(describe_scan(conflicts(bindings)))
    return issues
