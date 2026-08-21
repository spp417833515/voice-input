---
brief: 解析 "ctrl+shift+z" 字符串 → (X11 modifier mask, keysym) 二元组
tags: [复用, 函数库, hotkey, x11]
logic_flow: |
  flowchart TD
    In["hotkey_str: str"] --> Split["lower().strip().split('+')"]
    Split --> Loop["for mod in parts[:-1]:"]
    Loop --> Map["ctrl→ControlMask alt→Mod1<br/>shift→ShiftMask super→Mod4"]
    Map --> Key["key_name = parts[-1]"]
    Key --> KS["XK.string_to_keysym(key_name)"]
    KS --> Cap{"keysym==0?"}
    Cap -->|是| Title["再试 key_name.capitalize()"]
    Cap -->|否| Out([(mask, keysym)])
    Title --> Out
---

# `_parse_hotkey`

## 表格区块

| 区块 | 内容 |
| :--- | :--- |
| 一句话定位 | 把 `"ctrl+shift+z"` 风格的字符串解析成 X11 的 `(ControlMask\|ShiftMask, XK_z)` 二元组 |
| 一行调用示例 | `mask, keysym = _parse_hotkey("ctrl+grave")` |
| 入参 | `hotkey_str: str` — `"modifier+...+key"` 格式；modifier 支持 `ctrl / alt / shift / super`；key 用 XK 标准名 (`grave`, `x`, `z`, `F1`, `Escape`...) |
| 返回 | `tuple[int, int]` — `(X11 modifier mask, keysym int)` |
| 使用场景 + 陷阱 | **场景**: `XInputHotkeyGrabber._compile_hotkey` 和 `HotkeyGrabber.__init__` 各调一次解析三个快捷键 (HOTKEY_VOICE / HOTKEY_SCREENSHOT / HOTKEY_REPEAT)。**陷阱**: ① keysym 大小写敏感，`XK.string_to_keysym("z")` 可以但 `"Z"` 不行，所以失败时回退 `capitalize()`；② 不支持 `cmd / meta` 别名，super 必须写 `super`；③ 不校验顺序也不去重（"ctrl+ctrl+a" 不会报错）。 |

## 定义位置

`daemon.py:250-276`

## 被调用位置（满足 L5 复用门槛 ≥2）

- `daemon.py:1648-1650` `HotkeyGrabber.__init__` — 解析 voice/screen/repeat 三个快捷键
- `daemon.py:1853` `XInputHotkeyGrabber._compile_hotkey` — 同样三次，但额外把 mask 转成 keycode set
