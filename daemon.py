#!/usr/bin/env python3
"""全局快捷键守护进程 —— 按住录音 / 截图翻译 / 常驻悬浮状态栏。

快捷键:
  Ctrl+`       按住录音，松开转写并自动粘贴到当前光标
  Alt+X        截图框选，OCR 翻译（结果浮窗显示在选区附近）

使用 XGrabKey (X11 服务器级别) 注册快捷键，最高优先级。
常驻悬浮窗显示实时状态。

需要系统工具: xclip/xsel，以及 maim 或 grim+slurp
启动: ./install.sh && ./start.sh
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Wayland 兼容: 自动检测会话类型，Wayland 下启用 XWayland 回退
# 必须在 import gi 之前设置 GDK_BACKEND，否则 GTK 已按 wayland 初始化
# ---------------------------------------------------------------------------
_SESSION_TYPE = os.environ.get("XDG_SESSION_TYPE", "x11")
if _SESSION_TYPE == "wayland" and os.environ.get("DISPLAY"):
    # XWayland 可用，强制 GTK 使用 X11 后端以保证 move()/XGrabKey 正常
    # 必须用赋值而非 setdefault，因为桌面环境已预设 GDK_BACKEND=wayland
    os.environ["GDK_BACKEND"] = "x11"

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

# 系统托盘支持 (AyatanaAppIndicator3 → AppIndicator3 → 无托盘回退)
_HAS_INDICATOR = False
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3

    _HAS_INDICATOR = True
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3

        _HAS_INDICATOR = True
    except (ValueError, ImportError):
        pass

import httpx
import numpy as np
import sounddevice as sd
import soundfile as sf
try:
    from pynput.keyboard import Controller as KbController
    from pynput.keyboard import Key
    _pynput_available = True
except Exception:
    _pynput_available = False
from Xlib import X, XK
from Xlib import display as xdisplay


# ---------------------------------------------------------------------------
# 配置（持久化到 .config.json）
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / ".config.json"
APP_ICON = str(BASE_DIR / "resources" / "ollama-voice-input.svg")
VERSION = "2.2.0"

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:17945")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "zh")
SAMPLE_RATE = 16000

# 截图翻译目标语言（可通过 .config.json 自定义）
TRANSLATE_TARGET_LANG = "zh"

# 快捷键配置（可通过 .config.json 自定义）
HOTKEY_VOICE = "ctrl+grave"
HOTKEY_SCREENSHOT = "alt+x"
HOTKEY_REPEAT = "ctrl+shift+z"


def load_config() -> None:
    """从 .config.json 加载持久化配置，覆盖环境变量默认值。"""
    global \
        OLLAMA_MODEL, \
        WHISPER_LANGUAGE, \
        HOTKEY_VOICE, \
        HOTKEY_SCREENSHOT, \
        HOTKEY_REPEAT, \
        TRANSLATE_TARGET_LANG
    if not CONFIG_PATH.exists():
        return
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        OLLAMA_MODEL = cfg.get("ollama_model", OLLAMA_MODEL)
        WHISPER_LANGUAGE = cfg.get("whisper_language", WHISPER_LANGUAGE)
        HOTKEY_VOICE = cfg.get("hotkey_voice", HOTKEY_VOICE)
        HOTKEY_SCREENSHOT = cfg.get("hotkey_screenshot", HOTKEY_SCREENSHOT)
        HOTKEY_REPEAT = cfg.get("hotkey_repeat", HOTKEY_REPEAT)
        TRANSLATE_TARGET_LANG = cfg.get("translate_target_lang", TRANSLATE_TARGET_LANG)
    except Exception as e:
        print(f"  ⚠ 配置文件加载失败，使用默认值: {e}", flush=True)


def save_config() -> None:
    """将当前配置写入 .config.json。"""
    cfg = {
        "ollama_model": OLLAMA_MODEL,
        "whisper_language": WHISPER_LANGUAGE,
        "hotkey_voice": HOTKEY_VOICE,
        "hotkey_screenshot": HOTKEY_SCREENSHOT,
        "hotkey_repeat": HOTKEY_REPEAT,
        "translate_target_lang": TRANSLATE_TARGET_LANG,
    }
    try:
        tmp_path = CONFIG_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        tmp_path.rename(CONFIG_PATH)  # 原子替换（同文件系统）
    except Exception as e:
        print(f"  ⚠ 配置保存失败: {e}", flush=True)


# 全局状态栏引用（main() 中初始化）
_status_bar: StatusBar | None = None
_tray_indicator = None  # 系统托盘图标

# 全局翻译覆盖层引用
_translation_overlay: TranslationOverlay | None = None

# 全局快捷键抓取器引用（main() 中初始化）
_hotkey_grabber: HotkeyGrabber | None = None

# 全局 uinput 虚拟键盘 —— 内核级按键事件，不可被应用检测（main() 中初始化）
_uinput = None

# xdotool / wtype 路径 —— 用于合成粘贴按键
_xdotool = shutil.which("xdotool")
_wtype = shutil.which("wtype")

# 上一次语音识别结果（用于 Ctrl+Shift+Z 重复输入）
_last_result = ""

# IBus 输入法总线（main() 中初始化）—— 用于直接提交文字到焦点窗口
_ibus_bus = None
_ibus_commit_available = False
_ibus_commit_reason = "未初始化"

# 终端模拟器 WM_CLASS 名称集合（用于智能粘贴快捷键选择）
_TERMINALS = frozenset(
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
    }
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

_kb = KbController() if _pynput_available else None


def set_status(text: str, style: str = "normal") -> None:
    """线程安全地更新悬浮状态栏。style: normal, recording, busy, error, success"""
    if _status_bar:
        GLib.idle_add(_status_bar.update_status, text, style)


def _reset_status_later(delay_ms: int = 3000) -> None:
    """延迟恢复状态栏到就绪状态（仅在无活动任务时生效）。"""
    GLib.timeout_add(delay_ms, lambda: (
        set_status("就绪")
        if not _screenshot_busy
        and not (_hotkey_grabber and _hotkey_grabber.voice_active)
        else None,
        False,
    )[-1])


def _request_regrab() -> bool:
    """在主线程中刷新快捷键抓取，防止合成按键导致 XGrabKey 失效。"""
    if _hotkey_grabber and not _hotkey_grabber.voice_active:
        try:
            # 不做 ungrab，直接 grab 覆盖，避免热键注册空白期
            _hotkey_grabber.grab()
        except Exception:
            pass
    return False  # 单次执行


def _play_beep(freq: float = 440, duration: float = 0.1, volume: float = 0.3) -> None:
    """播放简短提示音（非阻塞）。用于录音开始/结束的听觉反馈。"""
    try:
        t = np.linspace(0, duration, int(SAMPLE_RATE * duration), endpoint=False)
        wave = volume * np.sin(2 * np.pi * freq * t).astype(np.float32)
        # 淡入淡出避免爆音
        fade = min(int(SAMPLE_RATE * 0.01), len(wave) // 4)
        wave[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
        wave[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        sd.play(wave, SAMPLE_RATE)
    except Exception:
        pass  # 提示音非关键功能，失败不影响录音


def _parse_hotkey(hotkey_str: str) -> tuple[int, int]:
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
    key_name = parts[-1]
    keysym = XK.string_to_keysym(key_name)
    if keysym == 0:
        # 尝试首字母大写 (XK 标准: "z"→"z", "grave"→"grave", "F1"→"F1")
        keysym = XK.string_to_keysym(key_name.capitalize())
    return mask, keysym


def _format_hotkey(hotkey_str: str) -> str:
    """将配置字符串格式化为用户友好的显示文本。"""
    return (
        hotkey_str.replace("+", "+")
        .replace("ctrl", "Ctrl")
        .replace("alt", "Alt")
        .replace("shift", "Shift")
        .replace("super", "Super")
        .replace("grave", "`")
    )


def _parse_hotkey_evdev(hotkey_str: str) -> dict:
    """解析快捷键字符串为 evdev 格式 (内核级，Wayland 兼容)。

    返回: {'trigger': evdev_keycode, 'modifiers': [set_of_keycodes, ...]}
    """
    from evdev import ecodes

    parts = hotkey_str.lower().strip().split("+")

    mod_map = {
        "ctrl": {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL},
        "alt": {ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT},
        "shift": {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT},
        "super": {ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA},
    }

    key_map = {
        "grave": ecodes.KEY_GRAVE, "`": ecodes.KEY_GRAVE,
        "escape": ecodes.KEY_ESC, "esc": ecodes.KEY_ESC,
        "space": ecodes.KEY_SPACE,
        "tab": ecodes.KEY_TAB,
        "enter": ecodes.KEY_ENTER, "return": ecodes.KEY_ENTER,
        "backspace": ecodes.KEY_BACKSPACE,
        "delete": ecodes.KEY_DELETE,
        "minus": ecodes.KEY_MINUS, "equal": ecodes.KEY_EQUAL,
        "semicolon": ecodes.KEY_SEMICOLON,
        "comma": ecodes.KEY_COMMA, "dot": ecodes.KEY_DOT,
        "slash": ecodes.KEY_SLASH, "backslash": ecodes.KEY_BACKSLASH,
    }
    for c in range(ord("a"), ord("z") + 1):
        key_map[chr(c)] = getattr(ecodes, f"KEY_{chr(c).upper()}")
    for i in range(10):
        key_map[str(i)] = getattr(ecodes, f"KEY_{i}")
    for i in range(1, 13):
        key_map[f"f{i}"] = getattr(ecodes, f"KEY_F{i}")

    modifiers = []
    for mod in parts[:-1]:
        if mod in mod_map:
            modifiers.append(mod_map[mod])

    trigger_name = parts[-1]
    trigger = key_map.get(trigger_name)
    if trigger is None:
        raise ValueError(f"Unknown evdev key: {trigger_name}")

    return {"trigger": trigger, "modifiers": modifiers}


def copy_to_clipboard(text: str) -> bool:
    """跨平台剪贴板写入（优先 xclip，Mutter 自动桥接 X11↔Wayland 剪贴板）。

    daemon 以 GDK_BACKEND=x11 (XWayland) 运行：
    - xclip 写入 X11 CLIPBOARD → Mutter 自动桥接到 Wayland 剪贴板
    - wl-copy 在 GNOME 上需要创建临时窗口抢焦点获取 serial，
      导致开始菜单闪烁且可能因无法获取焦点而静默失败
    """
    if shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard"]
    elif shutil.which("xsel"):
        cmd = ["xsel", "--clipboard", "--input"]
    elif os.environ.get("XDG_SESSION_TYPE") == "wayland" and shutil.which("wl-copy"):
        cmd = ["wl-copy"]
    else:
        set_status("剪贴板工具缺失，请安装 xclip", "error")
        return False
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        proc.communicate(text.encode("utf-8"), timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        set_status("剪贴板写入超时", "error")
        return False
    if proc.returncode != 0:
        set_status("剪贴板写入失败", "error")
        return False
    return True


def _probe_ibus_commit_support(bus) -> tuple[bool, str]:
    """探测当前会话是否支持 IBus current_input_context 直提交流程。"""
    if not bus.is_connected():
        return False, "IBus 守护进程未连接"
    gdbus = shutil.which("gdbus")
    if gdbus:
        try:
            result = subprocess.run(
                [
                    gdbus,
                    "introspect",
                    "--session",
                    "--dest",
                    "org.freedesktop.IBus",
                    "--object-path",
                    "/org/freedesktop/IBus",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0:
                if "CurrentInputContext" in result.stdout:
                    return True, "D-Bus 已暴露 CurrentInputContext"
                return False, "D-Bus 未暴露 CurrentInputContext"
        except Exception:
            pass
    try:
        ctx_path = bus.current_input_context()
    except Exception as exc:
        detail = str(exc).splitlines()[0]
        return False, f"当前会话不支持 current_input_context ({detail})"
    if ctx_path:
        return True, f"已检测到输入上下文 {ctx_path}"
    return True, "当前会话支持 current_input_context，将按需尝试直接提交"


def _input_status_text(mode: str, repeated: bool = False) -> str:
    """根据输入模式返回用户可见状态。"""
    if mode == "direct":
        return "已重复输入" if repeated else "已输入"
    return "已发送重复" if repeated else "已发送粘贴"


def paste_from_clipboard() -> None:
    """模拟 Ctrl+Shift+V 把剪贴板内容粘贴到当前聚焦的应用。"""
    if _kb is None:
        return
    time.sleep(0.1)
    _kb.press(Key.ctrl)
    _kb.press(Key.shift)
    _kb.tap("v")
    _kb.release(Key.shift)
    _kb.release(Key.ctrl)


def _try_ibus_commit(text: str) -> bool:
    """Tier 1: 通过 IBus 输入法框架直接提交文字到焦点窗口的输入上下文。

    仅在确认支持 current_input_context 的 IBus 会话中启用。
    """
    global _ibus_bus, _ibus_commit_available, _ibus_commit_reason

    if _ibus_bus is None or not _ibus_commit_available:
        return False
    try:
        gi.require_version("IBus", "1.0")
        from gi.repository import IBus as IBusLib

        if not _ibus_bus.is_connected():
            return False
        ctx_path = _ibus_bus.current_input_context()
        if not ctx_path:
            return False
        ctx = IBusLib.InputContext.get_input_context(
            ctx_path, _ibus_bus.get_connection()
        )
        if ctx is None:
            return False
        ctx.commit_text(IBusLib.Text.new_from_string(text))
        return True
    except Exception as exc:
        detail = str(exc).splitlines()[0]
        _ibus_bus = None
        _ibus_commit_available = False
        _ibus_commit_reason = detail
        print(f"  IBus: 直接提交不可用，回退到剪贴板模式 ({detail})", flush=True)
        return False


def _is_terminal_focused() -> bool:
    """检测当前焦点窗口是否为终端模拟器（通过 X11 WM_CLASS 属性）。"""
    try:
        dpy = xdisplay.Display()
        try:
            focus = dpy.get_input_focus().focus
            if not focus or focus == X.NONE:
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
                    return inst in _TERMINALS or klass in _TERMINALS
                parent = window.query_tree().parent
                if parent == window:
                    break
                window = parent
        finally:
            dpy.close()
    except Exception:
        pass
    return False


def _type_text(text: str) -> str:
    """将文字输入到当前焦点窗口，返回实际采用的输入模式。

    Tier 1: IBus commit_text — 通过输入法框架直接提交（最可靠，无剪贴板副作用）
    Tier 2: xdotool --clearmodifiers — 正确保存/恢复修饰键状态，不破坏 XGrabKey
    Tier 3: uinput — 内核级按键（会破坏修饰键状态，可能导致快捷键暂时失效）
    Tier 4: pynput — XTest 合成按键回退
    """
    time.sleep(0.1)  # 等待焦点稳定

    # ---- Tier 1: IBus 输入法直接提交 ----
    if _try_ibus_commit(text):
        print(f"  输入: Tier1 IBus 直接提交成功 ({len(text)} 字)", flush=True)
        return "direct"

    # ---- 剪贴板粘贴 ----
    if not copy_to_clipboard(text):
        raise RuntimeError("剪贴板写入失败")
    # xclip → X11 CLIPBOARD → Mutter 桥接 → Wayland 剪贴板，需要足够延迟
    time.sleep(0.3)
    # 验证剪贴板是否写入成功
    clip_check = _get_clipboard_text()
    if clip_check != text:
        print(f"  输入: [WARN] 剪贴板验证失败! 期望='{text[:30]}' 实际='{clip_check[:30] if clip_check else 'None'}'", flush=True)
        # 重试一次
        if not copy_to_clipboard(text):
            raise RuntimeError("剪贴板重试写入失败")
        time.sleep(0.3)
    else:
        print(f"  输入: 剪贴板已写入 '{text[:30]}' ✓", flush=True)
    is_term = _is_terminal_focused()
    print(f"  输入: IBus 失败, 使用剪贴板粘贴 (终端={is_term}, 会话={_SESSION_TYPE})", flush=True)

    # Wayland 环境: uinput (内核级) 是唯一可靠的按键模拟方式
    # - wtype: GNOME 不支持 virtual keyboard protocol
    # - xdotool: 返回 0 但按键无法到达 Wayland 原生窗口
    # - pynput: XTest 合成，同样无法到达 Wayland 窗口
    # X11 环境: xdotool --clearmodifiers 最佳（正确保存/恢复修饰键状态）

    if _SESSION_TYPE == "wayland":
        # ---- Wayland Tier 2: uinput（内核级，唯一可靠方案）----
        # Wayland 下无法检测焦点窗口类型（GNOME 安全策略封锁了所有 API），
        # 所以始终发送 Ctrl+Shift+V：终端需要 Shift，其他应用视为"粘贴纯文本"
        if _uinput:
            try:
                from evdev import ecodes

                _uinput.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1)
                _uinput.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
                _uinput.write(ecodes.EV_KEY, ecodes.KEY_V, 1)
                _uinput.syn()
                time.sleep(0.05)
                _uinput.write(ecodes.EV_KEY, ecodes.KEY_V, 0)
                _uinput.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
                _uinput.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
                _uinput.syn()
                print("  输入: Tier2 uinput Ctrl+Shift+V 已发送", flush=True)
                return "paste"
            except Exception as e:
                print(f"  输入: Tier2 uinput 失败: {e}", flush=True)

        # ---- Wayland Tier 3: wtype 回退 ----
        if _wtype:
            try:
                r = subprocess.run(
                    [_wtype, "-M", "ctrl", "-M", "shift", "-k", "v",
                     "-m", "shift", "-m", "ctrl"],
                    timeout=3, check=False, capture_output=True,
                )
                if r.returncode == 0:
                    print("  输入: Tier3 wtype Ctrl+Shift+V 已发送", flush=True)
                    return "paste"
            except Exception:
                pass

    else:
        # ---- X11 Tier 2: xdotool（修饰键安全）----
        if _xdotool:
            try:
                combo = "ctrl+shift+v" if is_term else "ctrl+v"
                subprocess.run(
                    [_xdotool, "key", "--clearmodifiers", combo],
                    timeout=3, check=False,
                )
                print(f"  输入: Tier2 xdotool {combo} 已发送", flush=True)
                return "paste"
            except Exception:
                pass

        # ---- X11 Tier 3: uinput ----
        if _uinput:
            try:
                from evdev import ecodes

                _uinput.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1)
                if is_term:
                    _uinput.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
                _uinput.write(ecodes.EV_KEY, ecodes.KEY_V, 1)
                _uinput.syn()
                time.sleep(0.02)
                _uinput.write(ecodes.EV_KEY, ecodes.KEY_V, 0)
                if is_term:
                    _uinput.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
                _uinput.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
                _uinput.syn()
                print("  输入: Tier3 uinput 粘贴已发送", flush=True)
                return "paste"
            except Exception:
                pass

    # ---- Tier 4: pynput 回退（XTest，仅 X11）----
    if _kb is not None:
        try:
            _kb.press(Key.ctrl)
            if is_term:
                _kb.press(Key.shift)
            _kb.tap("v")
        finally:
            try:
                if is_term:
                    _kb.release(Key.shift)
                _kb.release(Key.ctrl)
            except Exception:
                pass
        print("  输入: Tier4 pynput 粘贴已发送", flush=True)
        return "paste"

    print("  输入: [ERROR] 所有输入方式均不可用！", flush=True)
    raise RuntimeError("无法模拟粘贴：xdotool/uinput/pynput 均不可用")


def _get_clipboard_text() -> str:
    """读取当前剪贴板文本内容（用于保存/恢复）。

    优先 xclip（与 copy_to_clipboard 保持一致，使用 X11 剪贴板）。
    """
    if shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-o"]
    elif shutil.which("xsel"):
        cmd = ["xsel", "--clipboard", "--output"]
    elif os.environ.get("XDG_SESSION_TYPE") == "wayland" and shutil.which("wl-paste"):
        cmd = ["wl-paste", "--no-newline"]
    else:
        return ""
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=2)
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


def detect_screenshot_tool() -> str | None:
    """检测可用的截图工具。"""
    session = os.environ.get("XDG_SESSION_TYPE", "x11")
    if session == "x11":
        if shutil.which("maim"):
            return "x11-maim"
    elif session == "wayland":
        if shutil.which("grim") and shutil.which("slurp"):
            return "wayland-grim"
    if shutil.which("gnome-screenshot"):
        return "gnome"
    return None


def _get_png_dimensions(path: str) -> tuple[int, int]:
    """从 PNG 文件头读取图片宽高（无需 PIL）。"""
    with open(path, "rb") as f:
        f.read(16)  # 跳过 PNG 签名 + IHDR chunk header
        w, h = struct.unpack(">II", f.read(8))
    return w, h


def _get_mouse_position() -> tuple[int, int] | None:
    """获取当前鼠标位置（可在非 GTK 线程调用）。"""
    try:
        dpy = xdisplay.Display()
        try:
            qp = dpy.screen().root.query_pointer()
            return (qp.root_x, qp.root_y)
        finally:
            dpy.close()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CSS 样式（状态栏 + 翻译浮窗共用）
# ---------------------------------------------------------------------------

ALL_CSS = """
.status-bar {
    background-color: rgba(15, 15, 25, 0.92);
    border-radius: 28px;
    border: 1px solid rgba(100, 140, 255, 0.25);
    padding: 10px 18px;
    box-shadow: 0 2px 16px rgba(0, 0, 0, 0.45);
}
.status-dot {
    min-width: 8px;
    min-height: 8px;
    border-radius: 4px;
    background-color: #4ade80;
}
.status-recording .status-dot { background-color: #ef4444; }
.status-busy .status-dot { background-color: #f59e0b; }
.status-error .status-dot { background-color: #ef4444; }
.status-success .status-dot { background-color: #22c55e; }
.status-icon {
    color: rgba(180, 200, 230, 0.85);
    font-size: 26px;
}
.status-recording .status-icon { color: #fca5a5; }
.status-busy .status-icon { color: #fcd34d; }
.status-error .status-icon { color: #fca5a5; }
.status-success .status-icon { color: #86efac; }
.status-label {
    color: #c0c8e0;
    font-size: 11px;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
}
.status-recording .status-label { color: #fca5a5; }
.status-busy .status-label { color: #fcd34d; }
.status-error .status-label { color: #fca5a5; }
.status-success .status-label { color: #86efac; }

/* 翻译结果覆盖层 */
.trans-overlay {
    background-color: rgba(18, 18, 28, 0.72);
    border-radius: 8px;
    border: 1.5px solid rgba(60, 140, 255, 0.3);
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
}
.trans-loading {
    color: rgba(200, 210, 230, 0.9);
    font-size: 14px;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    padding: 10px 16px;
}
.trans-overlay progressbar trough {
    min-height: 4px;
    background-color: rgba(255,255,255,0.08);
    border-radius: 2px;
    margin: 0 12px 8px 12px;
}
.trans-overlay progressbar progress {
    min-height: 4px;
    border-radius: 2px;
    background: linear-gradient(90deg, #3b82f6, #22d3ee);
}
.trans-text {
    color: #d8dff2;
    font-size: 15px;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    padding: 14px 18px;
}
.trans-error {
    color: #ff8888;
    font-size: 14px;
    padding: 14px 18px;
}
.trans-hint {
    color: rgba(180, 180, 195, 0.6);
    font-size: 11px;
    padding: 0 18px 10px 18px;
}
.trans-bottom {
    padding: 4px 14px 10px 14px;
}
.trans-copy-btn {
    background: rgba(60, 140, 255, 0.2);
    border: 1px solid rgba(60, 140, 255, 0.4);
    border-radius: 6px;
    color: #8cb4ff;
    padding: 4px 14px;
    font-size: 12px;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
}
.trans-copy-btn:hover {
    background: rgba(60, 140, 255, 0.35);
}
"""


# ---------------------------------------------------------------------------
# 常驻悬浮状态栏
# ---------------------------------------------------------------------------


class StatusBar(Gtk.Window):
    """常驻悬浮状态栏，左下角显示实时状态。"""

    def __init__(self) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)

        # 载入 CSS
        provider = Gtk.CssProvider()
        provider.load_from_data(ALL_CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # 窗口属性
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_accept_focus(False)

        # RGBA 透明
        screen = Gdk.Screen.get_default()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        # 主容器
        self.ebox = Gtk.EventBox()
        self.ebox.get_style_context().add_class("status-bar")
        self.add(self.ebox)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.set_halign(Gtk.Align.CENTER)
        self.ebox.add(vbox)

        # 状态圆点
        self.dot = Gtk.DrawingArea()
        self.dot.set_size_request(8, 8)
        self.dot.set_halign(Gtk.Align.CENTER)
        self.dot.get_style_context().add_class("status-dot")
        vbox.pack_start(self.dot, False, False, 0)

        # 麦克风图标
        self.icon_label = Gtk.Label(label="\U0001f3a4")
        self.icon_label.set_halign(Gtk.Align.CENTER)
        self.icon_label.get_style_context().add_class("status-icon")
        vbox.pack_start(self.icon_label, False, False, 0)

        # 状态文字（紧凑，仅显示短状态）
        self.label = Gtk.Label()
        self.label.get_style_context().add_class("status-label")
        self.label.set_justify(Gtk.Justification.CENTER)
        self.label.set_max_width_chars(8)
        self.label.set_line_wrap(True)
        self.label.set_text("就绪")
        vbox.pack_start(self.label, False, False, 0)

        # 拖拽
        self._dragging = False
        self._drag_offset = (0, 0)
        self.ebox.connect("button-press-event", self._on_press)
        self.ebox.connect("button-release-event", self._on_release)
        self.ebox.connect("motion-notify-event", self._on_motion)
        self.ebox.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )

        self.set_default_size(-1, -1)
        self.show_all()
        # 定位到左下角
        GLib.idle_add(self._position_bottom_left)

    def _position_bottom_left(self) -> bool:
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        alloc = self.get_allocation()
        x = geo.x + 20
        y = geo.y + geo.height - alloc.height - 50
        self.move(x, y)
        return False

    def update_status(self, text: str, style: str = "normal") -> None:
        # 图标切换
        icon_map = {
            "recording": "\U0001f534",   # 红圈
            "busy": "\u23f3",            # 沙漏
            "error": "\u26a0",           # 警告
            "success": "\u2713",         # 对勾
            "normal": "\U0001f3a4",      # 麦克风
        }
        self.icon_label.set_text(icon_map.get(style, "\U0001f3a4"))
        # 短状态文字
        short = text
        if len(text) > 8:
            # 截取关键词
            for kw in ("录音", "转写", "已输入", "截图", "失败", "太短", "排队", "识别", "就绪", "重复"):
                if kw in text:
                    short = kw
                    if kw == "录音":
                        short = "录音中"
                    elif kw == "转写":
                        short = "转写中"
                    break
            else:
                short = text[:6] + "…"
        self.label.set_text(short)
        ctx = self.ebox.get_style_context()
        for s in ("status-recording", "status-busy", "status-error", "status-success"):
            ctx.remove_class(s)
        if style != "normal":
            ctx.add_class(f"status-{style}")

    def _on_press(self, widget, event):
        if event.button == 1:
            # 手动拖拽（begin_move_drag 在 XWayland 上静默失败，不抛异常但 WM 不响应）
            self._dragging = True
            self._drag_offset = (event.x_root, event.y_root)
            self._win_pos = self.get_position()
        elif event.button == 3:
            menu = Gtk.Menu()
            item = Gtk.MenuItem(label="退出")
            item.connect("activate", lambda _: Gtk.main_quit())
            menu.append(item)
            menu.show_all()
            # 菜单关闭后重新抓取快捷键
            menu.connect("deactivate", lambda _: GLib.idle_add(_request_regrab))
            menu.popup(None, None, None, None, event.button, event.time)

    def _on_release(self, widget, event):
        if self._dragging:
            self._dragging = False
            # 拖拽结束后重新抓取快捷键，防止 X grab 状态丢失
            GLib.idle_add(_request_regrab)

    def _on_motion(self, widget, event):
        if self._dragging:
            dx = event.x_root - self._drag_offset[0]
            dy = event.y_root - self._drag_offset[1]
            ox, oy = self._win_pos
            self.move(int(ox + dx), int(oy + dy))


# ---------------------------------------------------------------------------
# 系统托盘图标
# ---------------------------------------------------------------------------


def _show_about(_item=None) -> None:
    """显示关于对话框。"""
    dialog = Gtk.AboutDialog()
    dialog.set_program_name("Ollama Voice Input")
    dialog.set_version(VERSION)
    dialog.set_comments("全局语音输入 + 截图翻译\n完全本地运行，隐私安全")
    dialog.set_authors(["pp"])
    dialog.set_website("https://github.com/spp417833515/ollama-voice-input")
    dialog.set_website_label("GitHub")
    dialog.set_copyright("QQ: 417833515")
    dialog.set_license_type(Gtk.License.MIT_X11)
    # 尝试加载自定义图标
    try:
        logo = GdkPixbuf.Pixbuf.new_from_file_at_scale(APP_ICON, 128, 128, True)
        dialog.set_logo(logo)
    except Exception:
        dialog.set_logo_icon_name("audio-input-microphone")
    dialog.run()
    dialog.destroy()


def _open_settings(_item=None) -> None:
    """打开设置页面。"""
    import webbrowser

    webbrowser.open(f"{API_BASE}/setup")


def _create_tray_icon() -> object | None:
    """创建系统托盘图标，返回 indicator 对象或 None。"""
    if not _HAS_INDICATOR:
        print("  托盘: AppIndicator3 不可用，跳过系统托盘")
        return None

    icon_path = APP_ICON if Path(APP_ICON).exists() else "audio-input-microphone"

    indicator = AppIndicator3.Indicator.new(
        "ollama-voice-input",
        icon_path,
        AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
    )
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
    indicator.set_title("Ollama Voice Input")

    menu = Gtk.Menu()

    # 显示/隐藏状态栏
    toggle = Gtk.CheckMenuItem(label="显示悬浮状态栏")
    toggle.set_active(True)

    def _toggle_bar(item):
        if _status_bar:
            if item.get_active():
                _status_bar.show_all()
            else:
                _status_bar.hide()

    toggle.connect("toggled", _toggle_bar)
    menu.append(toggle)

    menu.append(Gtk.SeparatorMenuItem())

    # 设置
    settings_item = Gtk.MenuItem(label="设置")
    settings_item.connect("activate", _open_settings)
    menu.append(settings_item)

    # 关于
    about_item = Gtk.MenuItem(label="关于")
    about_item.connect("activate", _show_about)
    menu.append(about_item)

    menu.append(Gtk.SeparatorMenuItem())

    # 退出
    quit_item = Gtk.MenuItem(label="退出")
    quit_item.connect("activate", lambda _: Gtk.main_quit())
    menu.append(quit_item)

    menu.show_all()
    indicator.set_menu(menu)

    print("  托盘: 系统托盘图标已创建 ✓")
    return indicator


# ---------------------------------------------------------------------------
# 翻译覆盖层（截图翻译后在选区原位显示，先加载动画再显示结果）
# ---------------------------------------------------------------------------


class TranslationOverlay(Gtk.Window):
    """翻译结果覆盖层，精确覆盖在截图选区上方。"""

    def __init__(self, x: int, y: int, w: int, h: int) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)

        self._timer = 0
        self._pulse_id = 0
        self._text = ""
        self._sel_x, self._sel_y = x, y
        self._sel_w, self._sel_h = w, h

        # 窗口属性
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(True)
        self.set_resizable(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.connect("key-press-event", self._on_key_press)
        self.connect("focus-out-event", self._on_focus_out)

        # RGBA 透明
        screen = Gdk.Screen.get_default()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        # 主容器
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._box.get_style_context().add_class("trans-overlay")

        # 加载状态
        self._loading_label = Gtk.Label(label="识别翻译中...")
        self._loading_label.get_style_context().add_class("trans-loading")
        self._loading_label.set_vexpand(True)
        self._loading_label.set_valign(Gtk.Align.CENTER)
        self._box.pack_start(self._loading_label, True, True, 0)

        self._progress = Gtk.ProgressBar()
        self._progress.set_pulse_step(0.08)
        self._box.pack_start(self._progress, False, False, 0)

        # EventBox 包裹（点击关闭，复制按钮除外）
        self._ebox = Gtk.EventBox()
        self._ebox.add(self._box)
        self._ebox.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self._ebox.connect("button-press-event", self._on_ebox_click)
        self._copy_btn = None  # 记录复制按钮，点击时不关闭
        self.add(self._ebox)

        # 定位到选区
        disp_w = max(w, 200)
        disp_h = max(h, 60)
        self.set_default_size(disp_w, disp_h)
        self.move(x, y)
        self.show_all()

        # 进度条脉冲动画
        self._pulse_id = GLib.timeout_add(100, self._pulse)
        self.connect("destroy", self._on_destroy)
        self.present()  # 获取焦点以接收键盘事件

    def _pulse(self) -> bool:
        self._progress.pulse()
        return True

    def _on_key_press(self, widget, event) -> bool:
        """Esc 键关闭覆盖层。"""
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _on_focus_out(self, widget, event) -> bool:
        """失去焦点时自动关闭（点击窗口外任意区域）。"""
        self.destroy()
        return False

    def _on_ebox_click(self, widget, event) -> bool:
        """点击覆盖层任意位置关闭，复制按钮除外。"""
        if self._copy_btn and self._copy_btn.get_mapped():
            # 检查点击是否落在复制按钮区域内
            btn_alloc = self._copy_btn.get_allocation()
            # 将窗口坐标转换为按钮相对坐标
            ok, bx, by = self._copy_btn.translate_coordinates(widget, 0, 0)
            if ok:
                if bx <= event.x <= bx + btn_alloc.width and by <= event.y <= by + btn_alloc.height:
                    return False  # 不拦截，让按钮处理
        self.destroy()
        return True

    def show_result(self, text: str, is_error: bool = False) -> None:
        """从加载状态切换到结果显示。"""
        self._text = text

        # 停止脉冲
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0

        # 清空加载控件
        for child in self._box.get_children():
            child.destroy()

        # 翻译文本
        label = Gtk.Label()
        label.set_text(text)
        label.set_line_wrap(True)
        label.set_max_width_chars(60)
        label.set_xalign(0)
        label.set_selectable(False)
        label.get_style_context().add_class("trans-error" if is_error else "trans-text")
        self._box.pack_start(label, True, True, 0)

        # 底部：复制按钮 + 提示
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottom.get_style_context().add_class("trans-bottom")

        if not is_error:
            copy_btn = Gtk.Button(label="\U0001f4cb 复制")
            copy_btn.get_style_context().add_class("trans-copy-btn")
            copy_btn.connect("clicked", self._on_copy)
            self._copy_btn = copy_btn
            bottom.pack_start(copy_btn, False, False, 0)

        hint = Gtk.Label(label="点击任意位置 / Esc 关闭")
        hint.get_style_context().add_class("trans-hint")
        bottom.pack_start(hint, False, False, 0)

        self._box.pack_start(bottom, False, False, 0)

        self.show_all()

        # 调整尺寸：保持宽度不小于选区，高度自适应
        nat_w = max(self._sel_w, self.get_preferred_width()[1], 200)
        nat_h = max(self._sel_h, self.get_preferred_height()[1], 60)
        self.resize(nat_w, nat_h)

        # 30s 自动关闭
        self._timer = GLib.timeout_add(30000, self._auto_close)

    def _on_copy(self, btn) -> None:
        copy_to_clipboard(self._text)
        btn.set_label("已复制 \u2713")
        btn.set_sensitive(False)

    def _auto_close(self) -> bool:
        self.destroy()
        return False

    def _on_destroy(self, *args) -> None:
        global _translation_overlay
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        _translation_overlay = None


def _show_overlay_loading(x: int, y: int, w: int, h: int) -> bool:
    """GTK 主线程：创建加载中的覆盖层。"""
    global _translation_overlay
    if _translation_overlay is not None:
        try:
            _translation_overlay.destroy()
        except Exception:
            pass
        _translation_overlay = None
    _translation_overlay = TranslationOverlay(x, y, w, h)
    return False


def _update_overlay_result(text: str, is_error: bool = False) -> bool:
    """GTK 主线程：更新覆盖层为翻译结果。"""
    if _translation_overlay is not None:
        _translation_overlay.show_result(text, is_error)
    return False


# ---------------------------------------------------------------------------
# 语音录制
# ---------------------------------------------------------------------------


class VoiceRecorder:
    def __init__(self) -> None:
        self.recording = False
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._submit_queue: queue.Queue[np.ndarray] = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def start(self) -> bool:
        """开始录音。成功返回 True，失败返回 False。"""
        with self._lock:
            if self.recording:
                return True
            self.recording = True
            self._frames = []

        set_status("录音中... 松开停止", "recording")

        def callback(indata, frames, time_info, status):
            if self.recording:
                self._frames.append(indata.copy())

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=callback,
            )
            self._stream.start()
            _play_beep(400, 0.1)  # 低音提示: 开始录音（流启动后再播，避免设备冲突）
        except Exception as exc:
            # 音频设备打开失败，重置状态避免永久卡死
            print(f"[VoiceRecorder] 音频流启动失败: {exc}", file=sys.stderr)
            with self._lock:
                self.recording = False
            if self._stream:
                try:
                    self._stream.close()
                except Exception:
                    pass
            self._stream = None
            set_status(f"麦克风打开失败", "error")
            _reset_status_later()
            return False
        return True

    def stop(self) -> None:
        with self._lock:
            if not self.recording:
                return
            self.recording = False

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        _play_beep(700, 0.1)  # 高音提示: 结束录音

        if not self._frames:
            set_status("没有录到声音", "error")
            _reset_status_later()
            return

        audio = np.concatenate(self._frames, axis=0)
        # 极短录音（<0.15s）视为按键弹跳，不浪费 API 调用
        duration = len(audio) / SAMPLE_RATE
        print(f"  录音: 时长 {duration:.2f}s, 采样 {len(audio)} 帧", flush=True)
        if duration < 0.15:
            set_status("就绪")
            return
        # 其余录音全部进入队列，由 Whisper 判断是否有内容
        self._submit_queue.put(audio)
        pending = self._submit_queue.qsize()
        if pending > 1:
            set_status(f"已排队，前方 {pending - 1} 条处理中...", "busy")
        else:
            set_status("转写中...", "busy")

    def _worker_loop(self) -> None:
        """后台工作线程：从队列中顺序取出音频并处理。"""
        while True:
            audio = self._submit_queue.get()
            try:
                self._process_audio(audio)
            except Exception:
                import traceback

                print(f"[VoiceRecorder] 处理异常:", file=sys.stderr)
                traceback.print_exc()
            finally:
                self._submit_queue.task_done()

    def _process_audio(self, audio: np.ndarray) -> None:
        """处理单条录音：保存 WAV → API 转写 → 粘贴结果。"""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_path = f.name
                sf.write(f, audio, SAMPLE_RATE)

            pending = self._submit_queue.qsize()
            if pending > 0:
                set_status(f"转写中...(排队 {pending} 条)", "busy")
            else:
                set_status("转写中...", "busy")

            with open(tmp_path, "rb") as f:
                files = {"audio": ("recording.wav", f, "audio/wav")}
                data = {"mode": "raw", "language": WHISPER_LANGUAGE}
                resp = httpx.post(
                    f"{API_BASE}/api/transcribe",
                    files=files,
                    data=data,
                    timeout=httpx.Timeout(5.0, connect=5.0, read=120.0),
                )
                if resp.status_code != 200:
                    try:
                        detail = resp.json().get("detail", resp.text[:200])
                    except Exception:
                        detail = resp.text[:200]
                    raise RuntimeError(detail)

            result = resp.json()
            final = result.get("final_text", "")
            print(f"  转写: 结果='{final[:50]}' (status={resp.status_code})", flush=True)
            if final:
                # 末尾自动加句号
                if final[-1] not in "。！？.!?…；;:：,，、":
                    final += "。"
                global _last_result
                _last_result = final
                old_clipboard = _get_clipboard_text()
                input_mode = _type_text(final)
                time.sleep(0.3)
                # 仅在剪贴板仍是本次结果时恢复，避免覆盖用户新复制的内容
                current = _get_clipboard_text()
                if current == final:
                    copy_to_clipboard(old_clipboard)
                GLib.idle_add(_request_regrab)
                set_status(_input_status_text(input_mode), "success")
            else:
                set_status("没有识别到内容", "error")
        except Exception as exc:
            set_status(f"转写失败: {str(exc)[:60]}", "error")
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
            # 只有队列空了才设置"就绪"恢复定时器，避免覆盖后续任务的状态
            if self._submit_queue.empty():
                _q = self._submit_queue
                GLib.timeout_add(
                    3000,
                    lambda: (
                        set_status("就绪")
                        if _q.empty()
                        and not _screenshot_busy
                        and not (_hotkey_grabber and _hotkey_grabber.voice_active)
                        else None,
                        False,
                    )[-1],
                )


# ---------------------------------------------------------------------------
# 截图翻译（maim 选区 → 原位覆盖层显示加载 → OCR+翻译 → 覆盖层显示结果）
# ---------------------------------------------------------------------------

_screenshot_busy = False
_screenshot_lock = threading.Lock()


def screenshot_translate() -> None:
    """截图翻译：maim 选区 → tesseract OCR → Google 翻译 → 选区原位覆盖层。"""
    global _screenshot_busy
    with _screenshot_lock:
        if _screenshot_busy:
            set_status("截图翻译进行中，请等待", "busy")
            return
        _screenshot_busy = True

    # 在主线程立即启动 maim，消除线程调度延迟
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name
    proc = subprocess.Popen(
        ["maim", "-s", tmp_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    set_status("截图选择中...", "busy")
    threading.Thread(
        target=_do_screenshot_translate, args=(proc, tmp_path), daemon=True
    ).start()


def _do_screenshot_translate(proc: subprocess.Popen, tmp_path: str) -> None:
    """后台执行截图翻译流程（maim 进程已在主线程启动）。"""
    global _screenshot_busy
    try:
        # 1. 等待 maim 选区完成（进程已在主线程启动以消除延迟）
        if proc.wait() != 0:
            set_status("就绪")
            return

        # 2. 计算选区几何：图片尺寸 + 鼠标释放位置
        img_w, img_h = _get_png_dimensions(tmp_path)
        mouse_pos = _get_mouse_position()
        if mouse_pos:
            rx, ry = mouse_pos
            # 鼠标释放点通常是选区右下角（最常见的拖拽方向）
            sel_x = max(0, rx - img_w)
            sel_y = max(0, ry - img_h)
        else:
            sel_x, sel_y = 100, 100

        # 3. 立即在选区原位显示加载覆盖层
        GLib.idle_add(_show_overlay_loading, sel_x, sel_y, img_w, img_h)
        set_status("识别翻译中...", "busy")

        # 4. OCR + 翻译
        with open(tmp_path, "rb") as f:
            resp = httpx.post(
                f"{API_BASE}/api/ocr_translate",
                files={"image": ("crop.png", f, "image/png")},
                data={"target_lang": TRANSLATE_TARGET_LANG},
                timeout=120,
            )

        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", resp.text[:200])
            except Exception:
                detail = resp.text[:200]
            raise RuntimeError(detail)

        text = resp.json().get("text", "")
        if text:
            set_status("翻译完成", "success")
            GLib.idle_add(_update_overlay_result, text, False)
        else:
            set_status("没有识别到文字", "error")
            GLib.idle_add(_update_overlay_result, "没有识别到文字", True)
    except Exception as exc:
        set_status(f"截图失败: {str(exc)[:60]}", "error")
        GLib.idle_add(_update_overlay_result, f"失败: {str(exc)[:40]}", True)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        with _screenshot_lock:
            _screenshot_busy = False
        GLib.timeout_add(
            5000,
            lambda: (
                set_status("就绪")
                if not _screenshot_busy
                and not (_hotkey_grabber and _hotkey_grabber.voice_active)
                else None,
                False,
            )[-1],
        )


# ---------------------------------------------------------------------------
# XGrabKey 全局快捷键 (集成到 GTK 事件循环)
# ---------------------------------------------------------------------------

LOCK_MASKS = [
    0,
    X.Mod2Mask,  # NumLock
    X.LockMask,  # CapsLock
    X.Mod2Mask | X.LockMask,  # NumLock + CapsLock
]


class HotkeyGrabber:
    """XGrabKey 快捷键管理，通过 GLib.io_add_watch 集成到 GTK 事件循环。

    自动重复检测使用 GLib.timeout_add 延迟释放方案：
    - KeyRelease 时不立即执行，而是设置 50ms 定时器
    - 如果 50ms 内收到同键 KeyPress，说明是自动重复，取消定时器
    - 如果 50ms 后无 KeyPress，说明是真正释放，执行 stop
    """

    def __init__(self, recorder: VoiceRecorder) -> None:
        self.recorder = recorder
        self.voice_active = False
        self._pending_release_id = 0
        self.dpy = xdisplay.Display()
        self.dpy.set_error_handler(self._ignore_error)
        self.root = self.dpy.screen().root

        # 从配置解析快捷键
        v_mask, v_sym = _parse_hotkey(HOTKEY_VOICE)
        s_mask, s_sym = _parse_hotkey(HOTKEY_SCREENSHOT)
        r_mask, r_sym = _parse_hotkey(HOTKEY_REPEAT)

        self.voice_kc = self.dpy.keysym_to_keycode(v_sym)
        self.voice_mask = v_mask
        self.screen_kc = self.dpy.keysym_to_keycode(s_sym)
        self.screen_mask = s_mask
        self.repeat_kc = self.dpy.keysym_to_keycode(r_sym)
        self.repeat_mask = r_mask

    @staticmethod
    def _ignore_error(err, *args):
        pass

    def grab(self) -> None:
        for lock in LOCK_MASKS:
            self.root.grab_key(
                self.voice_kc,
                self.voice_mask | lock,
                True,
                X.GrabModeAsync,
                X.GrabModeAsync,
            )
            self.root.grab_key(
                self.screen_kc,
                self.screen_mask | lock,
                True,
                X.GrabModeAsync,
                X.GrabModeAsync,
            )
            self.root.grab_key(
                self.repeat_kc,
                self.repeat_mask | lock,
                True,
                X.GrabModeAsync,
                X.GrabModeAsync,
            )
        self.dpy.flush()

    def ungrab(self) -> None:
        for lock in LOCK_MASKS:
            self.root.ungrab_key(self.voice_kc, self.voice_mask | lock)
            self.root.ungrab_key(self.screen_kc, self.screen_mask | lock)
            self.root.ungrab_key(self.repeat_kc, self.repeat_mask | lock)
        self.dpy.flush()

    def start(self) -> None:
        self.grab()
        GLib.io_add_watch(self.dpy.fileno(), GLib.IO_IN, self._on_xlib_event)
        GLib.timeout_add(500, self._refresh_grab)

    def _refresh_grab(self) -> bool:
        if not self.voice_active:
            try:
                self.grab()  # XGrabKey 对同一客户端幂等，不做 ungrab 避免注册空白
            except Exception:
                pass
        return True

    def _handle_event(self, event) -> None:
        if event.type == X.KeyPress:
            if event.detail == self.voice_kc:
                if self._pending_release_id:
                    GLib.source_remove(self._pending_release_id)
                    self._pending_release_id = 0
                if not self.voice_active:
                    self.voice_active = True
                    if not self.recorder.start():
                        self.voice_active = False
            elif event.detail == self.screen_kc:
                screenshot_translate()
            elif event.detail == self.repeat_kc:
                self._repeat_last()
        elif event.type == X.KeyRelease:
            if event.detail == self.voice_kc and self.voice_active:
                if self._pending_release_id:
                    GLib.source_remove(self._pending_release_id)
                self._pending_release_id = GLib.timeout_add(50, self._do_release)

    def _do_release(self) -> bool:
        self._pending_release_id = 0
        if self.voice_active:
            self.voice_active = False
            self.recorder.stop()
            # 注意：此处不做 ungrab/grab，保持 XGrabKey 持续活跃
            # 避免在连续快速录音时出现热键注册空白期
            # 粘贴后的 _request_regrab + 500ms _refresh_grab 已覆盖防护
        return False

    def _repeat_last(self) -> None:
        """重复输入上一次的语音识别结果。"""
        if not _last_result:
            set_status("还没有识别记录", "error")
            _reset_status_later(2000)
            return
        set_status("重复输入中...", "busy")
        threading.Thread(target=self._do_repeat, daemon=True).start()

    def _do_repeat(self) -> None:
        """在后台线程执行重复输入（避免阻塞 GTK 主循环）。"""
        try:
            old_clipboard = _get_clipboard_text()
            input_mode = _type_text(_last_result)
            time.sleep(0.3)
            current = _get_clipboard_text()
            if current == _last_result:
                copy_to_clipboard(old_clipboard)
            GLib.idle_add(_request_regrab)
            set_status(_input_status_text(input_mode, repeated=True), "success")
        except Exception as exc:
            set_status(f"重复输入失败: {str(exc)[:40]}", "error")
        _reset_status_later()

    def _on_xlib_event(self, fd, condition) -> bool:
        try:
            self.dpy.sync()
        except Exception as e:
            print(f"XGrabKey sync error: {e}", file=sys.stderr)
            return True
        while self.dpy.pending_events() > 0:
            try:
                event = self.dpy.next_event()
                self._handle_event(event)
            except Exception as e:
                print(f"XGrabKey event error: {e}", file=sys.stderr)
        return True

    def cleanup(self) -> None:
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
        self.ungrab()
        self.dpy.close()


# ---------------------------------------------------------------------------
# evdev 全局快捷键 (内核级，适用于 X11 和 Wayland)
# ---------------------------------------------------------------------------


class EvdevHotkeyGrabber:
    """evdev 内核级全局快捷键，适用于 X11 和 Wayland。

    直接读取 /dev/input/event* 设备，绕过显示服务器限制。
    需要用户属于 input 组: sudo usermod -aG input $USER
    """

    def __init__(self, recorder: VoiceRecorder) -> None:
        from evdev import InputDevice, ecodes, list_devices

        self.recorder = recorder
        self.voice_active = False
        self._pending_release_id = 0
        self._running = False
        self._thread = None
        self._pressed_keys: set[int] = set()

        # 解析快捷键为 evdev 格式
        self._voice_combo = _parse_hotkey_evdev(HOTKEY_VOICE)
        self._screen_combo = _parse_hotkey_evdev(HOTKEY_SCREENSHOT)
        self._repeat_combo = _parse_hotkey_evdev(HOTKEY_REPEAT)
        print(f"  evdev: 语音快捷键 {HOTKEY_VOICE} → trigger={self._voice_combo['trigger']} mods={self._voice_combo['modifiers']}")
        print(f"  evdev: 截图快捷键 {HOTKEY_SCREENSHOT} → trigger={self._screen_combo['trigger']} mods={self._screen_combo['modifiers']}")
        print(f"  evdev: 重复快捷键 {HOTKEY_REPEAT} → trigger={self._repeat_combo['trigger']} mods={self._repeat_combo['modifiers']}")

        # 查找键盘设备（排除自身 uinput 虚拟键盘，避免反馈循环）
        self._devices: list = []
        for path in list_devices():
            try:
                dev = InputDevice(path)
                if dev.name == "ollama-voice-input":
                    dev.close()
                    continue
                caps = dev.capabilities()
                if ecodes.EV_KEY in caps:
                    keys = caps[ecodes.EV_KEY]
                    if ecodes.KEY_A in keys and ecodes.KEY_Z in keys:
                        self._devices.append(dev)
                        print(f"  evdev: 监控键盘 {dev.name} ({dev.path})")
            except Exception:
                pass

        if not self._devices:
            raise RuntimeError(
                "未找到键盘设备。请确保用户属于 input 组: "
                "sudo usermod -aG input $USER 然后重新登录"
            )

    def grab(self) -> None:
        pass  # evdev 不需要 XGrabKey 式的 grab

    def ungrab(self) -> None:
        pass

    def _rescan_devices(self) -> None:
        """重新扫描键盘设备（USB 热插拔恢复）。"""
        from evdev import InputDevice, ecodes, list_devices

        existing_paths = {d.path for d in self._devices}
        for path in list_devices():
            if path in existing_paths:
                continue
            try:
                dev = InputDevice(path)
                if dev.name == "ollama-voice-input":
                    dev.close()
                    continue
                caps = dev.capabilities()
                if ecodes.EV_KEY in caps:
                    keys = caps[ecodes.EV_KEY]
                    if ecodes.KEY_A in keys and ecodes.KEY_Z in keys:
                        self._devices.append(dev)
                        print(f"  evdev: 重新发现键盘 {dev.name} ({dev.path})", flush=True)
            except Exception:
                pass

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _monitor(self) -> None:
        import select

        from evdev import ecodes

        print("  evdev: 监控线程已启动", flush=True)
        print(f"  evdev: 监控 {len(self._devices)} 个设备, fds={[d.fd for d in self._devices]}", flush=True)

        while self._running:
            fds = {}
            for dev in list(self._devices):
                try:
                    fds[dev.fd] = dev
                except Exception as e:
                    print(f"  evdev: [ERROR] 获取fd失败 {dev.name}: {e}", flush=True)

            if not fds:
                print("  evdev: [WARN] 无可用设备, 尝试重新扫描...", flush=True)
                self._rescan_devices()
                time.sleep(3)
                continue

            try:
                r, _, _ = select.select(list(fds.keys()), [], [], 1.0)
            except Exception as e:
                print(f"  evdev: [ERROR] select 异常: {type(e).__name__}: {e}", flush=True)
                time.sleep(0.1)
                continue

            for fd in r:
                dev = fds.get(fd)
                if not dev:
                    continue
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        self._handle_key_event(event.code, event.value)
                except OSError as e:
                    print(f"  evdev: [ERROR] 设备断开 {dev.name}: {e}", flush=True)
                    try:
                        self._devices.remove(dev)
                    except ValueError:
                        pass
                except Exception as e:
                    print(f"  evdev: [ERROR] 事件读取异常: {type(e).__name__}: {e}", flush=True)

    def _handle_key_event(self, code: int, value: int) -> None:
        """处理按键事件。value: 0=释放, 1=按下, 2=自动重复"""
        if value == 1:  # 按下
            self._pressed_keys.add(code)
            self._check_press()
        elif value == 0:  # 释放
            self._pressed_keys.discard(code)
            if code == self._voice_combo["trigger"] and self.voice_active:
                print("  evdev: 语音触发键释放, 停止录音", flush=True)
                GLib.idle_add(self._schedule_release)
        # value == 2 (自动重复) → 忽略

    def _match_combo(self, combo: dict) -> bool:
        """检查当前按键状态是否匹配快捷键组合。"""
        if combo["trigger"] not in self._pressed_keys:
            return False
        for mod_keys in combo["modifiers"]:
            if not (mod_keys & self._pressed_keys):
                return False
        return True

    def _check_press(self) -> None:
        if self._match_combo(self._voice_combo):
            print("  evdev: ★ 语音快捷键匹配！调度录音", flush=True)
            GLib.idle_add(self._on_voice_press)
        elif self._match_combo(self._screen_combo):
            print("  evdev: ★ 截图快捷键匹配！调度截图", flush=True)
            GLib.idle_add(screenshot_translate)
        elif self._match_combo(self._repeat_combo):
            print("  evdev: ★ 重复快捷键匹配！调度重复", flush=True)
            GLib.idle_add(self._on_repeat)

    def _on_voice_press(self) -> bool:
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
        if not self.voice_active:
            self.voice_active = True
            if not self.recorder.start():
                print("  录音: 启动失败！", flush=True)
                self.voice_active = False
            else:
                print("  录音: 已开始录音 ✓", flush=True)
        return False

    def _schedule_release(self) -> bool:
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
        self._pending_release_id = GLib.timeout_add(50, self._do_release)
        return False

    def _do_release(self) -> bool:
        self._pending_release_id = 0
        if self.voice_active:
            self.voice_active = False
            self.recorder.stop()
        return False

    def _on_repeat(self) -> bool:
        if not _last_result:
            set_status("还没有识别记录", "error")
            _reset_status_later(2000)
            return False
        set_status("重复输入中...", "busy")
        threading.Thread(target=self._do_repeat_input, daemon=True).start()
        return False

    def _do_repeat_input(self) -> None:
        try:
            old_clipboard = _get_clipboard_text()
            input_mode = _type_text(_last_result)
            time.sleep(0.3)
            current = _get_clipboard_text()
            if current == _last_result:
                copy_to_clipboard(old_clipboard)
            set_status(_input_status_text(input_mode, repeated=True), "success")
        except Exception as exc:
            set_status(f"重复输入失败: {str(exc)[:40]}", "error")
        _reset_status_later()

    def cleanup(self) -> None:
        self._running = False
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
        if self._thread:
            self._thread.join(timeout=2)
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------


def startup_check() -> list[str]:
    issues = []

    session = os.environ.get("XDG_SESSION_TYPE", "x11")
    gdk_backend = os.environ.get("GDK_BACKEND", "auto")

    # Wayland 兼容性检查
    if session == "wayland":
        if not os.environ.get("DISPLAY"):
            issues.append(
                "Wayland 环境下未检测到 XWayland（$DISPLAY 未设置），"
                "快捷键和窗口拖拽可能不可用。请安装 xwayland"
            )
        elif gdk_backend not in ("x11",):
            issues.append(
                f"Wayland 环境但 GDK_BACKEND={gdk_backend}，"
                "拖拽/快捷键可能异常。建议设置 GDK_BACKEND=x11"
            )
        if not shutil.which("xclip") and not shutil.which("xsel"):
            issues.append("Wayland 环境缺少 xclip，请安装: sudo apt install xclip")
    else:
        if not shutil.which("xclip") and not shutil.which("xsel"):
            issues.append("缺少剪贴板工具: sudo apt install xclip")
        if not shutil.which("xdotool"):
            issues.append("建议安装 xdotool 以提高快捷键可靠性: sudo apt install xdotool")

    if not detect_screenshot_tool():
        if session == "wayland":
            issues.append("缺少截图工具: sudo apt install grim slurp")
        else:
            issues.append("缺少截图工具: sudo apt install maim")

    try:
        resp = httpx.get(f"{API_BASE}/api/setup/check", timeout=3)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ollama", {}).get("running"):
            issues.append("Ollama 服务未连接，请先运行 ollama serve")
    except Exception:
        issues.append(f"API 服务器不可达: {API_BASE}")

    return issues


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _acquire_singleton_lock() -> int:
    """获取单例锁，确保只有一个守护进程运行。返回锁文件 fd（进程退出自动释放）。"""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    lock_path = Path(runtime_dir) / "ollama-voice-input.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # 读取已有进程的 PID
        try:
            existing_pid = os.read(fd, 32).decode().strip()
        except Exception:
            existing_pid = "unknown"
        os.close(fd)
        print(f"错误: 守护进程已在运行 (PID {existing_pid})，不能重复启动。")
        print("  如需重启，请先停止现有进程: systemctl --user restart ollama-voice-daemon")
        sys.exit(1)
    # 写入当前 PID
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode())
    return fd  # fd 必须保持打开，进程退出时内核自动释放 flock


def main() -> None:
    global _status_bar, _hotkey_grabber, _uinput, _ibus_bus, _tray_indicator
    global _ibus_commit_available, _ibus_commit_reason

    # 单例锁 — 阻止重复启动
    _lock_fd = _acquire_singleton_lock()  # noqa: F841  (fd 必须保持引用防止 GC 关闭)

    # 加载持久化配置
    load_config()

    issues = startup_check()

    print(f"Ollama Voice Input v{VERSION} 守护进程已启动")
    print(f"  会话类型: {_SESSION_TYPE}")
    print(f"  GDK 后端: {os.environ.get('GDK_BACKEND', 'auto')}")
    print(
        f"  语音输入: {_format_hotkey(HOTKEY_VOICE)}  按住说话，松开自动粘贴"
    )
    print(f"  截图翻译: {_format_hotkey(HOTKEY_SCREENSHOT)}   框选截图，OCR+翻译")
    print(f"  重复输入: {_format_hotkey(HOTKEY_REPEAT)}  重复上一次识别结果")
    print(f"  API 地址: {API_BASE}")

    if issues:
        print("\n  ⚠ 配置检查:")
        for issue in issues:
            print(f"    - {issue}")

    print("\n悬浮状态栏已显示，右键状态栏可退出")

    # 创建状态栏
    _status_bar = StatusBar()

    # 创建系统托盘图标
    _tray_indicator = _create_tray_icon()

    # 初始化 uinput 虚拟键盘（内核级按键，不可被应用检测）
    try:
        from evdev import UInput, ecodes

        # 完整键盘能力声明：libinput/Mutter 需要足够多的按键才能将设备识别为键盘
        # 仅声明 3 个键会被归类为按钮设备，合成按键不会被分发到焦点窗口
        _all_keys = list(range(1, 249))  # KEY_ESC(1) ~ KEY_MICMUTE(248)
        _uinput = UInput(
            {ecodes.EV_KEY: _all_keys},
            name="ollama-voice-input",
        )
        print("  uinput: 已初始化（内核级按键模式）")
    except Exception as e:
        print(f"  uinput: 不可用，使用 pynput 回退 ({e})")
        _uinput = None
        if _SESSION_TYPE == "wayland":
            print("  ⚠ 警告: Wayland 下 uinput 不可用将导致粘贴功能失效！")
            print("    请确保: sudo chmod 660 /dev/uinput && sudo chgrp input /dev/uinput")

    if _xdotool:
        print("  xdotool: 已找到（修饰键安全粘贴模式）")
    else:
        print("  xdotool: 未安装，粘贴时可能影响快捷键（sudo apt install xdotool）")

    # 初始化 IBus 输入法总线（用于 Tier 1 直接提交文字）
    try:
        gi.require_version("IBus", "1.0")
        from gi.repository import IBus as IBusLib

        _ibus_bus_tmp = IBusLib.Bus()
        if _ibus_bus_tmp.is_connected():
            available, note = _probe_ibus_commit_support(_ibus_bus_tmp)
            if available:
                _ibus_bus = _ibus_bus_tmp
                _ibus_commit_available = True
                _ibus_commit_reason = note
                print(f"  IBus: 直接提交可用（{note}）")
            else:
                _ibus_commit_available = False
                _ibus_commit_reason = note
                print(f"  IBus: 当前会话不支持直接提交，使用剪贴板模式（{note}）")
        else:
            _ibus_commit_reason = "IBus 守护进程未运行"
            print("  IBus: 守护进程未运行，使用剪贴板模式")
    except Exception as e:
        _ibus_commit_reason = str(e)
        print(f"  IBus: 不可用 ({e})，使用剪贴板模式")

    # 初始化快捷键 — evdev 优先（内核级，Wayland 兼容），XGrabKey 回退
    recorder = VoiceRecorder()
    try:
        grabber = EvdevHotkeyGrabber(recorder)
        print("  快捷键: evdev 内核级模式（全平台兼容）")
    except Exception as e:
        print(f"  evdev 不可用 ({e})，回退到 XGrabKey")
        grabber = HotkeyGrabber(recorder)
        print("  快捷键: XGrabKey 模式（仅 X11）")
    _hotkey_grabber = grabber
    grabber.start()

    # SIGTERM 处理 — systemctl stop 时触发干净退出，确保 finally 块执行清理
    signal.signal(signal.SIGTERM, lambda s, f: GLib.idle_add(Gtk.main_quit))

    # GTK 主循环
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        grabber.cleanup()
        if _uinput:
            try:
                _uinput.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
