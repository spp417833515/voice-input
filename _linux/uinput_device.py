"""本进程 uinput 虚拟键盘。内核级按键，不触碰用户物理修饰键。"""
from __future__ import annotations

# XInput2 raw 的 sourceid 过滤与 evdev 设备过滤都用同一名字，避免自注入回流。
UINPUT_DEVICE_NAME = "ollama-voice-input"


def open_uinput():
    """创建虚拟键盘。失败抛异常，由调用方决定回退并打印原因。

    完整键盘能力声明：libinput/Mutter 需要足够多的按键才能将设备识别为键盘；
    仅声明 3 个键会被归类为按钮设备，合成按键不会被分发到焦点窗口。
    """
    from evdev import UInput, ecodes

    all_keys = list(range(1, 249))  # KEY_ESC(1) ~ KEY_MICMUTE(248)
    return UInput({ecodes.EV_KEY: all_keys}, name=UINPUT_DEVICE_NAME)
