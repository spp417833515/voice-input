---
brief: XInput2 RawKey 全局快捷键，与 HotkeyGrabber/EvdevHotkeyGrabber 接口同构 (start/cleanup/voice_active/grab/ungrab)
tags: [复用, 类库, hotkey, xinput2, x11, grabber]
logic_flow: |
  flowchart TD
    Init["__init__(recorder)"] --> Disp["xdisplay.Display()<br/>+ has_extension('XInputExtension')"]
    Disp --> Ver["xinput_query_version → ≥ 2.0"]
    Ver --> Parser["ge_add_event_data 注册 RawKeyPress/Release parser"]
    Parser --> Compile["_compile_hotkey x3<br/>(voice/screen/repeat)<br/>→ {trigger:kc, modifiers:[set]}"]

    Start["start()"] --> Select["root.xinput_select_events<br/>(AllMasterDevices, RawKeyPress|RawKeyRelease)"]
    Select --> Watch["GLib.io_add_watch(fd, IO_IN, _on_xlib_event)"]

    Event["_on_xlib_event"] --> Src{"sourceid ∈ ollama-voice-input?"}
    Src -->|是| Ign[忽略注入回流]
    Src -->|否| Press["_handle_press(kc)<br/>→ _match_combo(combo, pressed_kc=kc)"]
    Event --> Rel["_handle_release(keycode)<br/>→ 50ms 定时 _do_release"]

    Cleanup["cleanup()"] --> Close["dpy.close()"]
---

# `XInputHotkeyGrabber`

## 表格区块

| 区块 | 内容 |
| :--- | :--- |
| 一句话定位 | 用 XInput2 RawKey 被动监听设备层按键事件，构建全局快捷键 grabber；与 `HotkeyGrabber` / `EvdevHotkeyGrabber` 接口同构 |
| 一行调用示例 | `grabber = XInputHotkeyGrabber(recorder); grabber.start()` |
| 入参 | `recorder: VoiceRecorder` — 注入语音录音器，按键按下时调用 `recorder.start()`，释放时调 `recorder.stop()` |
| 返回 | 实例属性 `voice_active: bool` 给外部代码读取（如 `_process_audio` 用它判断是否仍在录音以决定状态栏复位时机，daemon.py:1486） |
| 使用场景 + 陷阱 | **场景**: `main()` 三级 fallback 第一顺位。**陷阱**: ① `grab/ungrab` 是 no-op；② raw 无 modifier mask，`_pressed` 只做自动重复/抬起，命中必须 `pressed_kc == trigger`；③ 只滤设备名 `ollama-voice-input` 的 sourceid，**不能**滤 XTEST（Synergy 走那条）；找不到该设备时才回退 `_inject_until`；④ 录音中触发键自动重复会取消挂起的 release 定时器。 |

## 定义位置

`daemon.py:1797-1986`

## 接口契约（与 HotkeyGrabber/EvdevHotkeyGrabber 同构）

| 成员 | 说明 |
| :--- | :--- |
| `__init__(recorder: VoiceRecorder)` | 解析三个快捷键、注册 XInput2 parser；不可用则抛 RuntimeError |
| `start() -> None` | 启动监听（select_events + io_add_watch） |
| `cleanup() -> None` | 移除挂起定时器 + 关闭 Display |
| `grab() -> None` | no-op |
| `ungrab() -> None` | no-op |
| `voice_active: bool` | 当前是否处于语音录音状态 |

## 被调用位置（接口同构关系）

- `daemon.py:2402` `main()` fallback 链第一顺位
- 接口契约伙伴（≥2 处证明复用判定）：
  - `daemon.py:1630` `HotkeyGrabber`（XGrabKey 版同接口）
  - `daemon.py:1994` `EvdevHotkeyGrabber`（evdev 版同接口）
