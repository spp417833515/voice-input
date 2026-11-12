"""X11 快捷键解析（XGrabKey / XInput2 用）。"""
from __future__ import annotations

from Xlib import X, XK

from _hotkey import KEY_TABLE, canonical_key

LOCK_MASKS = [
    0,
    X.Mod2Mask,  # NumLock
    X.LockMask,  # CapsLock
    X.Mod2Mask | X.LockMask,  # NumLock + CapsLock
]


def parse_hotkey(hotkey_str: str) -> tuple[int, int]:
    """解析快捷键字符串为 (X11 modifier mask, keysym)。

    格式: "modifier+...+key"
    修饰符: ctrl, alt, shift, super
    键名: XK 标准名 (grave, x, z, F1, Escape...)

    例: "ctrl+grave" → (ControlMask, XK_grave)
        "ctrl+shift+z" → (ControlMask|ShiftMask, XK_z)
    """
    parts = hotkey_str.lower().strip().split("+")
    mask = 0
    for mod in parts[:-1]:
        if mod == "ctrl":
            mask |= X.ControlMask
        elif mod == "alt":
            mask |= X.Mod1Mask
        elif mod == "shift":
            mask |= X.ShiftMask
        elif mod == "super":
            mask |= X.Mod4Mask
    known = KEY_TABLE.get(canonical_key(parts[-1]))
    key_name = known.keysym if known else parts[-1]  # 规范主键取唯一键名表；未知名沿用 XK 原样/首字母大写查找
    keysym = XK.string_to_keysym(key_name)
    if keysym == 0:
        # 尝试首字母大写 (XK 标准: "z"→"z", "grave"→"grave", "F1"→"F1")
        keysym = XK.string_to_keysym(key_name.capitalize())
    return mask, keysym
