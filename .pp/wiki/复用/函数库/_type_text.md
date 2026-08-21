---
brief: 4-tier 输入注入 — IBus → xdotool/uinput → wtype/uinput → pynput，把任意文字粘贴到当前焦点窗口
tags: [复用, 函数库, paste, ibus, uinput, xdotool, pynput, wayland, x11]
logic_flow: |
  flowchart TD
    In["text: str"] --> Sleep["time.sleep(0.1) 等焦点稳定"]
    Sleep --> T1["Tier1: _try_ibus_commit(text)"]
    T1 -->|成功| RetDirect([return 'direct'])
    T1 -->|失败| Clip["copy_to_clipboard(text)<br/>+ 0.3s + 验证 + 重试一次"]
    Clip --> Session{"_SESSION_TYPE?"}

    Session -->|wayland| W2["Tier2 wayland: uinput Ctrl+Shift+V<br/>(daemon.py:562-585)"]
    W2 -->|失败| W3["Tier3 wayland: wtype Ctrl+Shift+V<br/>(daemon.py:587-599)"]

    Session -->|x11| X2["Tier2 x11: xdotool --clearmodifiers ctrl[+shift]+v<br/>(daemon.py:602-613)"]
    X2 -->|失败| X3["Tier3 x11: uinput Ctrl[+Shift]+V<br/>(daemon.py:615-643)"]

    W2 --> Ret([return 'paste'])
    W3 --> Ret
    X2 --> Ret
    X3 --> Ret

    W3 -->|失败| T4["Tier4: pynput XTest (仅X11)<br/>(daemon.py:646-669)"]
    X3 -->|失败| T4
    T4 --> Ret
    T4 -->|失败| Raise([RuntimeError])
---

# `_type_text`

## 表格区块

| 区块 | 内容 |
| :--- | :--- |
| 一句话定位 | 把任意文字注入到当前焦点窗口，四级 fallback (IBus commit / 剪贴板+模拟粘贴 / pynput)，并保证修饰键状态干净 |
| 一行调用示例 | `mode = _type_text("识别结果。")  # mode ∈ {"direct", "paste"}` |
| 入参 | `text: str` — 要注入的文字（任意 unicode） |
| 返回 | `str` — `"direct"` 表示走 IBus 直接 commit，`"paste"` 表示走剪贴板模拟 Ctrl(+Shift)+V；全失败抛 `RuntimeError` |
| 使用场景 + 陷阱 | **场景**: ① `VoiceRecorder._process_audio` 转写后输入（daemon.py:1463）；② `_do_repeat_common` 重复输入快捷键（daemon.py:1610）。**陷阱**: ① 终端检测 `_is_terminal_focused()` 仅在 X11 有效，Wayland 一律发 Ctrl+Shift+V（GNOME 安全策略封锁了焦点 API，注释 daemon.py:560-561）；② uinput/pynput 路径 finally 必调 `_force_clear_uinput_mods` / `_force_clear_pynput_mods`，否则异常中断会残留修饰键按下状态导致后续按键全错；③ 剪贴板有 0.3s 等待 + 一次重试，因为 xclip 写入 X11 CLIPBOARD 后需要 Mutter 桥接到 Wayland。 |

## 定义位置

`daemon.py:519-672`

## 被调用位置（满足 L5 复用门槛 ≥2）

- `daemon.py:1463` `VoiceRecorder._process_audio` — 转写成功后注入文字
- `daemon.py:1610` `_do_repeat_common` — 重复输入快捷键复用
