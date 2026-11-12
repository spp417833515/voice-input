"""系统剪贴板读写（xclip 优先）。失败返回明确原因，由调用方决定可见性。

daemon 以 GDK_BACKEND=x11 (XWayland) 运行：
- xclip 写入 X11 CLIPBOARD → Mutter 自动桥接到 Wayland 剪贴板
- wl-copy 在 GNOME 上需要创建临时窗口抢焦点获取 serial，
  导致开始菜单闪烁且可能因无法获取焦点而静默失败
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from typing import Protocol


class Clipboard(Protocol):
    def write(self, text: str) -> str | None: ...

    def read(self) -> str: ...


class SystemClipboard:
    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        environ: Mapping[str, str] | None = None,
        popen=subprocess.Popen,
        run=subprocess.run,
    ) -> None:
        self._which = which
        self._env = os.environ if environ is None else environ
        self._popen = popen
        self._run = run

    def _wayland(self) -> bool:
        return self._env.get("XDG_SESSION_TYPE") == "wayland"

    def write(self, text: str) -> str | None:
        """写入剪贴板。成功返回 None，失败返回用户可读原因。"""
        if self._which("xclip"):
            cmd = ["xclip", "-selection", "clipboard"]
        elif self._which("xsel"):
            cmd = ["xsel", "--clipboard", "--input"]
        elif self._wayland() and self._which("wl-copy"):
            cmd = ["wl-copy"]
        else:
            return "剪贴板工具缺失，请安装 xclip"
        proc = self._popen(cmd, stdin=subprocess.PIPE)
        try:
            proc.communicate(text.encode("utf-8"), timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return "剪贴板写入超时"
        if proc.returncode != 0:
            return "剪贴板写入失败"
        return None

    def read(self) -> str:
        """读取当前剪贴板文本内容（用于保存/恢复）。读不到返回空串。"""
        if self._which("xclip"):
            cmd = ["xclip", "-selection", "clipboard", "-o"]
        elif self._which("xsel"):
            cmd = ["xsel", "--clipboard", "--output"]
        elif self._wayland() and self._which("wl-paste"):
            cmd = ["wl-paste", "--no-newline"]
        else:
            return ""
        try:
            result = self._run(cmd, capture_output=True, timeout=2)
            if result.returncode == 0:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception:
            pass
        return ""

