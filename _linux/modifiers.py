"""修饰键幂等清理（uinput / XTest 两层）与修饰键物理状态查询。

根因（迁自旧 daemon）：本机键盘经 Synergy 注入（XTest），长按热键结束时偶尔丢失修饰键的
KeyRelease，使 Ctrl/Alt 卡在按下态；注入中途异常也会残留按下状态。所有清理都幂等：
内核/ X server 对"释放一颗未按下的键"是 no-op。
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time

_MOD_KEYSYM_NAMES = ("Control_L", "Control_R", "Shift_L", "Shift_R",
                     "Alt_L", "Alt_R", "Super_L", "Super_R")
_HOTKEY_MOD_KEYSYMS = {
    "ctrl": ("Control_L", "Control_R"),
    "alt": ("Alt_L", "Alt_R"),
    "shift": ("Shift_L", "Shift_R"),
    "super": ("Super_L", "Super_R"),
}


def _load_pynput():
    try:
        from pynput.keyboard import Controller, Key

        return Controller(), Key
    except Exception:
        return None, None


class ModifierJanitor:
    """持有 pynput 控制器、xdotool 路径与 uinput 句柄；只做修饰键相关的清理与查询。"""

    def __init__(self, *, session_type: str, uinput=None, kb=None, key=None, xdotool: str | None = None) -> None:
        self.session_type = session_type
        self.uinput = uinput
        if kb is None and key is None:
            kb, key = _load_pynput()
        self.kb = kb
        self.Key = key
        self.xdotool = xdotool if xdotool is not None else shutil.which("xdotool")
        # 修饰键实时状态轮询用的独立 X 连接（仅注入线程访问, 已由 InjectGuard.lock 串行化）
        self._mod_poll_dpy = None
        self._mod_keycodes: frozenset[int] | None = None

    # ---- XTest 层 -------------------------------------------------------
    def clear_pynput(self) -> None:
        """pynput XTest 路径的修饰键兜底清零, 每个 release 独立 try, 互不影响."""
        if self.kb is None:
            return
        Key = self.Key
        for k in (Key.ctrl, Key.ctrl_r, Key.shift, Key.shift_r,
                  Key.alt, Key.alt_r, Key.cmd, Key.cmd_r):
            try:
                self.kb.release(k)
            except Exception:
                pass

    def clear_xtest(self) -> None:
        """XTest 层（pynput / xdotool）修饰键幂等清零。

        这是 XTest 注入卡键的**唯一自愈通道** —— uinput 层清理只作用于自己的虚拟
        设备，够不到 xdotool/pynput 写入 X server 核心键盘状态的"幽灵按下"。
        优先 pynput（进程内, 快）；无 pynput 时用 xdotool keyup 兜底（放线程执行,
        避免在 GTK 主线程/看门狗回调里被子进程阻塞）。
        """
        if self.kb is not None:
            self.clear_pynput()
            return
        if self.xdotool:
            xdotool = self.xdotool

            def _run() -> None:
                try:
                    subprocess.run(
                        [xdotool, "keyup",
                         "Control_L", "Control_R", "Alt_L", "Alt_R",
                         "Shift_L", "Shift_R", "Super_L", "Super_R"],
                        timeout=2, check=False,
                    )
                except Exception:
                    pass
            threading.Thread(target=_run, daemon=True).start()

    def release_hotkey(self, hotkey_str: str) -> None:
        """按住说话结束时，补发一次该热键所含修饰键的释放（XTest 层）。

        只释放本热键声明的修饰键（如 ctrl+alt），仅在录音结束时调用；
        释放一颗已松开的键是 no-op，不影响用户其它修饰键操作。
        """
        names = [n for tok in hotkey_str.lower().split("+")[:-1]
                 for n in _HOTKEY_MOD_KEYSYMS.get(tok.strip(), ())]
        if not names:
            return
        # 优先 pynput（XTest，进程内，不阻塞 GTK 主循环）
        if self.kb is not None:
            Key = self.Key
            py = {
                "Control_L": Key.ctrl, "Control_R": Key.ctrl_r,
                "Alt_L": Key.alt, "Alt_R": Key.alt_r,
                "Shift_L": Key.shift, "Shift_R": Key.shift_r,
                "Super_L": Key.cmd, "Super_R": Key.cmd_r,
            }
            for n in names:
                try:
                    self.kb.release(py[n])
                except Exception:
                    pass
            return
        # 回退 xdotool（子进程放线程里，避免阻塞主循环）
        if self.xdotool:
            xdotool = self.xdotool
            threading.Thread(
                target=lambda: subprocess.run(
                    [xdotool, "keyup", *names], timeout=2, check=False
                ),
                daemon=True,
            ).start()

    # ---- uinput 层 ------------------------------------------------------
    def clear_uinput(self) -> None:
        """对所有修饰键发一次幂等 release, 防止注入中途异常残留按下状态.

        覆盖: Ctrl/Shift/Alt/Super 各左右两侧共 8 个 keycode.
        """
        if not self.uinput:
            return
        try:
            from evdev import ecodes
            for code in (
                ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL,
                ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
                ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT,
                ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
            ):
                try:
                    self.uinput.write(ecodes.EV_KEY, code, 0)
                except Exception:
                    pass
            try:
                self.uinput.syn()
            except Exception:
                pass
        except Exception:
            pass

    # ---- 指针 grab -------------------------------------------------------
    @staticmethod
    def release_seat_grab() -> None:
        """解除任何遗留的 seat 指针 grab（仅本客户端持有的 grab，无活跃时 no-op，无害）。"""
        try:
            import gi

            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk

            display = Gdk.Display.get_default()
            if display is not None:
                seat = display.get_default_seat()
                if seat is not None:
                    seat.ungrab()
        except Exception:
            pass

    # ---- 物理状态查询 ------------------------------------------------------
    def any_modifier_down(self) -> bool:
        """查询用户是否物理按住任一修饰键（Ctrl/Shift/Alt/Super）。

        直接读 X server 硬件级键盘位图（query_keymap），权威且不受事件顺序影响。
        Wayland 无法查询 → 返回 False（不阻断注入）；任何异常同样返回 False。
        """
        if self.session_type == "wayland":
            return False
        try:
            if self._mod_poll_dpy is None:
                from Xlib import XK
                from Xlib import display as xdisplay

                self._mod_poll_dpy = xdisplay.Display()
                codes = set()
                for name in _MOD_KEYSYM_NAMES:
                    ks = XK.string_to_keysym(name)
                    if ks:
                        kc = self._mod_poll_dpy.keysym_to_keycode(ks)
                        if kc:
                            codes.add(kc)
                self._mod_keycodes = frozenset(codes)
            km = self._mod_poll_dpy.query_keymap()
            for kc in (self._mod_keycodes or ()):
                if km[kc >> 3] & (1 << (kc & 7)):
                    return True
            return False
        except Exception:
            return False

    def wait_released(self, timeout: float = 1.0) -> None:
        """注入前等待用户物理修饰键真正松开（带超时）。

        热键是"按住 Ctrl 说话"，注入常发生在用户还按着 Ctrl 时。若此刻注入粘贴键，
        会与用户按住的修饰键组合成误触发的快捷键。先等干净再注入；超时则继续（不阻断），仅打印告警。
        """
        deadline = time.monotonic() + timeout
        while self.any_modifier_down():
            if time.monotonic() >= deadline:
                print("  输入: 等待修饰键释放超时, 仍继续注入", flush=True)
                return
            time.sleep(0.02)

    def panic_release_all(self) -> None:
        """兜底释放：清掉 uinput + XTest 残留修饰键 + 解除任何遗留的 seat 指针 grab。

        全部幂等，可在任意时刻安全调用（atexit / 信号退出 / 空闲 watchdog / 启动）。
        浮窗/选区的关闭由调用方在此之后各自处理。
        """
        self.clear_uinput()
        self.clear_xtest()
        self.release_seat_grab()
