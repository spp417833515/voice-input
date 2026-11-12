"""焦点探测。平台只报告当前焦点，不决定是否润色或入队。

X11 返回顶层窗口 XID；Wayland 或失败为 0（未知）。未知时不得把注入直接判 failed。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

FocusDecision = Literal["match", "mismatch", "unknown"]

# 终端模拟器 WM_CLASS 名称集合（用于智能粘贴快捷键选择）
TERMINALS = frozenset(
    {
        "gnome-terminal-server",
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "alacritty",
        "kitty",
        "xterm",
        "uxterm",
        "terminator",
        "tilix",
        "st",
        "st-256color",
        "urxvt",
        "rxvt",
        "wezterm-gui",
        "wezterm",
        "foot",
        "sakura",
        "guake",
        "tilda",
        "yakuake",
        "lxterminal",
        "mate-terminal",
        "terminology",
        "qterminal",
        "deepin-terminal",
        "hyper",
        "tabby",
        "cool-retro-term",
        "ptyxis",
        "org.gnome.ptyxis",
        "ghostty",
        "com.mitchellh.ghostty",
        "warp",
        "dev.warp.warp",
        "contour",
        "blackbox",
        "black-box",
        "com.raggesilver.blackbox",
        "rio",
        "wave",
    }
)


class FocusProbe(Protocol):
    def current_id(self) -> int: ...

    def is_terminal(self) -> bool: ...


@dataclass(frozen=True)
class FocusSnapshot:
    focus_id: int
    terminal: bool = False


def decide_focus(expected: int | str | None, current: int | str | None) -> FocusDecision:
    """比较绑定焦点与当前焦点。任一侧空/0 为 unknown，不得误拦。"""
    left = _as_id(expected)
    right = _as_id(current)
    if left == 0 or right == 0:
        return "unknown"
    return "match" if left == right else "mismatch"


def _as_id(value: int | str | None) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class StaticFocusProbe:
    """测试用。不读真实窗口。"""

    def __init__(self, focus_id: int = 0, terminal: bool = False) -> None:
        self.focus_id = focus_id
        self.terminal = terminal

    def current_id(self) -> int:
        return self.focus_id

    def is_terminal(self) -> bool:
        return self.terminal

    def snapshot(self) -> FocusSnapshot:
        return FocusSnapshot(focus_id=self.focus_id, terminal=self.terminal)


class X11FocusProbe:
    """从现有 daemon 行为抽出的 X11 探测。测试应注入 Fake/Static，避免碰用户窗口。"""

    def __init__(self, *, session_type: str | None = None, display_factory=None) -> None:
        import os

        self.session_type = session_type or os.environ.get("XDG_SESSION_TYPE", "")
        self._display_factory = display_factory

    def current_id(self) -> int:
        if self.session_type == "wayland":
            return 0
        if self._display_factory is None:
            try:
                from Xlib import display as xdisplay
            except Exception:
                return 0
            factory = xdisplay.Display
        else:
            factory = self._display_factory
        try:
            dpy = factory()
        except Exception:
            return 0
        try:
            focus = dpy.get_input_focus().focus
            if not focus or isinstance(focus, int):
                return 0
            root = dpy.screen().root
            window = focus
            for _ in range(32):
                parent = window.query_tree().parent
                if parent is None or parent == window or parent == root:
                    break
                window = parent
            return int(window.id)
        except Exception:
            return 0
        finally:
            try:
                dpy.close()
            except Exception:
                pass

    def is_terminal(self) -> bool:
        """检测当前焦点窗口是否为终端模拟器（X11 WM_CLASS），决定 Ctrl+V 还是 Ctrl+Shift+V。"""
        if self.session_type == "wayland":
            return False
        factory = self._display_factory
        if factory is None:
            try:
                from Xlib import display as xdisplay
            except Exception:
                return False
            factory = xdisplay.Display
        try:
            dpy = factory()
        except Exception:
            return False
        try:
            focus = dpy.get_input_focus().focus
            if not focus or isinstance(focus, int):
                return False
            # 焦点可能在子窗口，向上遍历查找 WM_CLASS
            window = focus
            for _ in range(10):
                try:
                    cls = window.get_wm_class()
                except Exception:
                    break
                if cls:
                    inst, klass = cls[0].lower(), cls[1].lower()
                    return inst in TERMINALS or klass in TERMINALS
                parent = window.query_tree().parent
                if parent is None or parent == window:
                    break
                window = parent
        except Exception:
            return False
        finally:
            try:
                dpy.close()
            except Exception:
                pass
        return False

    def snapshot(self) -> FocusSnapshot:
        return FocusSnapshot(focus_id=self.current_id(), terminal=self.is_terminal())
