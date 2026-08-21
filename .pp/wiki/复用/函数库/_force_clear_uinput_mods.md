---
brief: 对所有 8 个修饰键 keycode 发一次幂等 release，防止 uinput 注入异常残留按下状态
tags: [复用, 函数库, uinput, modifier-leak, safety]
logic_flow: |
  flowchart TD
    In["_force_clear_uinput_mods()"] --> Check{"_uinput 已初始化?"}
    Check -->|否| Done([no-op])
    Check -->|是| Loop["for code in (LEFTCTRL,RIGHTCTRL,<br/>LEFTSHIFT,RIGHTSHIFT,<br/>LEFTALT,RIGHTALT,<br/>LEFTMETA,RIGHTMETA):"]
    Loop --> Write["_uinput.write(EV_KEY, code, 0)<br/>(value=0 即 release)"]
    Write --> Syn["_uinput.syn()"]
    Syn --> Done
---

# `_force_clear_uinput_mods`

## 表格区块

| 区块 | 内容 |
| :--- | :--- |
| 一句话定位 | 给 uinput 虚拟键盘发 8 个修饰键的 release 事件作为兜底，确保异常路径不会泄漏「按下」状态到内核 |
| 一行调用示例 | `_force_clear_uinput_mods()` |
| 入参 | 无 |
| 返回 | 无（None）；任何异常都被吞 |
| 使用场景 + 陷阱 | **场景**: 在 `_type_text` 的 Wayland Tier2 uinput 和 X11 Tier3 uinput 的 `finally` 块里被调用（daemon.py:582 / daemon.py:640），保证即便 Ctrl/Shift/V 序列中途抛异常也能把修饰键清零。**陷阱**: ① 必须晚于显式 release 循环再调用，否则与"反向 release 已按下键"的顺序冲突没意义；② 内核对 release 一颗未按下的键是 no-op，多发无副作用（这是它能幂等的理由，源码注释 daemon.py:493-494）；③ 仅清除 Ctrl/Shift/Alt/Meta 各左右 8 颗，**不清** Caps/Num/Scroll Lock（这些是锁定型，发 release 会让 LED 闪）。 |

## 定义位置

`daemon.py:491-516`

## 被调用位置（满足 L5 复用门槛 ≥2）

- `daemon.py:582` `_type_text` Wayland Tier2 uinput 路径 `finally`
- `daemon.py:640` `_type_text` X11 Tier3 uinput 路径 `finally`
