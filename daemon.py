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

import atexit
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
import traceback
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
from Xlib.ext import xinput as xi_ext
from Xlib.protocol import rq as xrq


# ---------------------------------------------------------------------------
# 配置（持久化到 .config.json）
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / ".config.json"
APP_ICON = str(BASE_DIR / "resources" / "ollama-voice-input.svg")
VERSION = "2.2.0"

API_BASE = os.getenv("API_BASE", "https://127.0.0.1:17945")
# 本机 https 用自签证书，daemon→本机不校验证书（localhost 自签，标准做法）
API_VERIFY_TLS = False
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
        try:
            os.chmod(CONFIG_PATH, 0o600)  # 与 app.py 一致，禁止其他本地用户读取
        except Exception:
            pass
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

# 全局注入锁 —— 串行化所有按键注入(_type_text)与修饰键清理(panic/watchdog)。
# 杜绝转写线程 / 重复输入线程 / 看门狗三方并发写同一 uinput / XTest 造成的"撕键"：
# 一方按下 Ctrl 还没发 V 时被另一方 release，粘贴退化成裸 v，或 X 层修饰键卡死。
# 用 RLock：_type_text 持锁期间其 finally 仍会调 _force_clear_*（同线程重入）。
_inject_lock = threading.RLock()

# 注入保护窗口：此刻之前抓取器忽略按键事件，防止自己注入的 Ctrl/Shift/V 经
# XInput2 raw / evdev 回流后再次匹配热键 → 触发录音/重复输入的自反馈回环。
_inject_until = 0.0

# 录音最长时限（秒）：release 丢失导致 voice_active 卡死时，看门狗据此强制复位，
# 避免"永久录音中 + 自愈系统被 voice_active 永久旁路"。
VOICE_MAX_SECONDS = 120.0

# 智能卡键检测阈值（针对 Synergy/XTest 丢 key-up 的幽灵修饰键）：
# 只在"修饰键真按下 + (持续超 SOFT 秒且键盘空闲超 IDLE 秒)"或"持续超 HARD 秒"时
# 才判定卡住并清理 —— 绝不误伤用户正常的长按修饰键。
STUCK_MOD_HARD_SECONDS = 6.0   # 无条件判定：没人会正常按住修饰键这么久
STUCK_MOD_SOFT_SECONDS = 2.5
STUCK_MOD_IDLE_SECONDS = 1.5   # 键盘无新按键事件（空闲）
# 思路②：打字(按非修饰键)时，若某修饰键已持续按下超此秒数，即时清理(比看门狗更快)。
# 正常"按住 Ctrl 连敲"通常是秒内连击，很少按住 Ctrl 超 3s 才敲键，故误伤概率极低。
STUCK_MOD_KEYPRESS_SECONDS = 3.0
# 热键匹配用：修饰键位图显示按下但持续超过此秒数 → 视为幽灵卡键，不参与命中。
# 正常组合是"先按修饰键再按触发键"，间隔几乎总 <1s；幽灵 Ctrl 常挂数分钟。
# 宁可要求用户重按修饰键，也不要用卡死的 Ctrl 把 Alt+Z 误判成 Ctrl+Alt+Z。
STUCK_MOD_MATCH_SECONDS = 3.0

# 录音开始瞬间的焦点顶层窗口 XID。注入前比对当前焦点，若已切走则跳过粘贴，
# 杜绝"说话时在 A 窗口、转写完成时焦点在 B 窗口 → 文字粘到 B"的错窗问题。
_record_focus_xid = 0

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


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

_kb = KbController() if _pynput_available else None


def set_status(text: str, style: str = "normal") -> None:
    """线程安全地更新悬浮状态栏。style: normal, recording, busy, error, success"""
    if _status_bar:
        GLib.idle_add(_status_bar.update_status, text, style)


def _screenshot_active() -> bool:
    """是否有截图进程仍在运行 (None / 已死 → False)。"""
    p = _screenshot_proc
    return p is not None and p.poll() is None


_reset_timer_id = 0


def _reset_status_later(delay_ms: int = 3000) -> None:
    """延迟恢复状态栏到就绪状态（仅在无活动任务时生效）。

    记录并取消上一个待触发的恢复定时器，避免多次调用叠加多个定时器互相覆盖
    （旧定时器把新的"转写中/录音中"错误刷成"就绪"→ 用户误判状态）。
    """
    global _reset_timer_id
    if _reset_timer_id:
        try:
            GLib.source_remove(_reset_timer_id)
        except Exception:
            pass
        _reset_timer_id = 0

    def _do() -> bool:
        global _reset_timer_id
        _reset_timer_id = 0
        if not _screenshot_active() and not (
            _hotkey_grabber and _hotkey_grabber.voice_active
        ):
            set_status("就绪")
        return False

    _reset_timer_id = GLib.timeout_add(delay_ms, _do)


def _play_beep(freq: float = 440, duration: float = 0.1, volume: float = 0.3) -> None:
    """播放简短提示音。整体放后台线程执行。

    sd.play 的"非阻塞"只指播放本身，其内部 **同步打开输出流**（PortAudio
    Pa_OpenStream）；设备忙 / ALSA 独占 / Pulse 恢复时可阻塞数百毫秒~数秒。
    而 beep 从录音开始/结束（GTK 主线程的按键回调）触发，一旦阻塞主循环，
    按键的按下/松开处理全部延迟 → 用户体感"按键卡住"。故整体移出主线程。
    """
    def _worker() -> None:
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
    threading.Thread(target=_worker, daemon=True).start()


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


def _get_focus_xid() -> int:
    """返回当前输入焦点的顶层窗口 XID（X11）。

    Wayland 或任何失败返回 0，表示"未知"—— 调用方据此跳过焦点校验（宁可不拦，
    也不误拦）。向上遍历到顶层，避免子控件焦点变化造成的误判。
    """
    if _SESSION_TYPE == "wayland":
        return 0
    try:
        dpy = xdisplay.Display()
        try:
            focus = dpy.get_input_focus().focus
            if not focus or isinstance(focus, int):
                return 0  # X.NONE(0) / PointerRoot(1) 等，无有效窗口
            root = dpy.screen().root
            window = focus
            for _ in range(32):
                parent = window.query_tree().parent
                if parent is None or parent == window or parent == root:
                    break
                window = parent
            return int(window.id)
        finally:
            dpy.close()
    except Exception:
        return 0


def _force_clear_pynput_mods() -> None:
    """pynput XTest 路径的修饰键兜底清零, 每个 release 独立 try, 互不影响."""
    if _kb is None:
        return
    for k in (Key.ctrl, Key.ctrl_r, Key.shift, Key.shift_r,
              Key.alt, Key.alt_r, Key.cmd, Key.cmd_r):
        try:
            _kb.release(k)
        except Exception:
            pass


def _force_clear_xtest_mods() -> None:
    """XTest 层（pynput / xdotool）修饰键幂等清零。

    这是 XTest 注入卡键的**唯一自愈通道** —— uinput 层清理只作用于自己的虚拟
    设备，够不到 xdotool/pynput 写入 X server 核心键盘状态的"幽灵按下"。
    优先 pynput（进程内, 快）；无 pynput 时用 xdotool keyup 兜底（放线程执行,
    避免在 GTK 主线程/看门狗回调里被子进程阻塞）。
    """
    if _kb is not None:
        _force_clear_pynput_mods()
        return
    if _xdotool:
        def _run() -> None:
            try:
                subprocess.run(
                    [_xdotool, "keyup",
                     "Control_L", "Control_R", "Alt_L", "Alt_R",
                     "Shift_L", "Shift_R", "Super_L", "Super_R"],
                    timeout=2, check=False,
                )
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()


# 修饰键实时状态轮询用的独立 X 连接（仅注入线程访问, 已由 _inject_lock 串行化）
_mod_poll_dpy = None
_mod_keycodes: frozenset[int] | None = None


def _any_modifier_down() -> bool:
    """查询用户是否物理按住任一修饰键（Ctrl/Shift/Alt/Super）。

    直接读 X server 硬件级键盘位图（query_keymap），权威且不受事件顺序影响。
    Wayland 无法查询 → 返回 False（不阻断注入）；任何异常同样返回 False。
    """
    global _mod_poll_dpy, _mod_keycodes
    if _SESSION_TYPE == "wayland":
        return False
    try:
        if _mod_poll_dpy is None:
            _mod_poll_dpy = xdisplay.Display()
            codes = set()
            for name in ("Control_L", "Control_R", "Shift_L", "Shift_R",
                         "Alt_L", "Alt_R", "Super_L", "Super_R"):
                ks = XK.string_to_keysym(name)
                if ks:
                    kc = _mod_poll_dpy.keysym_to_keycode(ks)
                    if kc:
                        codes.add(kc)
            _mod_keycodes = frozenset(codes)
        km = _mod_poll_dpy.query_keymap()
        for kc in (_mod_keycodes or ()):
            if km[kc >> 3] & (1 << (kc & 7)):
                return True
        return False
    except Exception:
        return False


def _wait_modifiers_released(timeout: float = 1.0) -> None:
    """注入前等待用户物理修饰键真正松开（带超时）。

    热键是"按住 Ctrl 说话"，注入常发生在用户还按着 Ctrl 时。若此刻注入粘贴键，
    会与用户按住的修饰键组合成误触发的快捷键；X11 旧路径的 --clearmodifiers 更
    会在"恢复期重按"里把用户已松开的 Ctrl 补按后无人释放 → 永久卡键。
    先等干净再注入，从根上消除这两类竞态。超时则继续（不阻断），仅打印告警。
    """
    deadline = time.monotonic() + timeout
    while _any_modifier_down():
        if time.monotonic() >= deadline:
            print("  输入: 等待修饰键释放超时, 仍继续注入", flush=True)
            return
        time.sleep(0.02)


def _release_hotkey_modifiers(hotkey_str: str) -> None:
    """按住说话结束时，补发一次该热键所含修饰键的释放（XTest 层）。

    根因：本机键盘经 Synergy 注入（XTest），长按热键结束时偶尔丢失修饰键的
    KeyRelease，使 Ctrl/Alt 卡在按下态 → 后续点击全变 Ctrl+拖拽、粘贴失效。
    对策：只释放本热键声明的修饰键（如 ctrl+alt），仅在录音结束时调用；
    释放一颗已松开的键是 no-op，不影响用户其它修饰键操作。
    """
    _KS = {
        "ctrl": ("Control_L", "Control_R"),
        "alt": ("Alt_L", "Alt_R"),
        "shift": ("Shift_L", "Shift_R"),
        "super": ("Super_L", "Super_R"),
    }
    names = [n for tok in hotkey_str.lower().split("+")[:-1]
             for n in _KS.get(tok.strip(), ())]
    if not names:
        return
    # 优先 pynput（XTest，进程内，不阻塞 GTK 主循环）
    if _kb is not None:
        _PY = {
            "Control_L": Key.ctrl, "Control_R": Key.ctrl_r,
            "Alt_L": Key.alt, "Alt_R": Key.alt_r,
            "Shift_L": Key.shift, "Shift_R": Key.shift_r,
            "Super_L": Key.cmd, "Super_R": Key.cmd_r,
        }
        for n in names:
            try:
                _kb.release(_PY[n])
            except Exception:
                pass
        return
    # 回退 xdotool（子进程放线程里，避免阻塞主循环）
    if _xdotool:
        threading.Thread(
            target=lambda: subprocess.run(
                [_xdotool, "keyup", *names], timeout=2, check=False
            ),
            daemon=True,
        ).start()


def _force_clear_uinput_mods() -> None:
    """对所有修饰键发一次幂等 release, 防止注入中途异常残留按下状态.

    内核对 release 一颗未按下的键是 no-op, 多发无副作用.
    覆盖: Ctrl/Shift/Alt/Super 各左右两侧共 8 个 keycode.
    """
    if not _uinput:
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
                _uinput.write(ecodes.EV_KEY, code, 0)
            except Exception:
                pass
        try:
            _uinput.syn()
        except Exception:
            pass
    except Exception:
        pass


def _panic_release_all() -> None:
    """兜底释放：清掉 uinput + XTest 残留修饰键 + 解除任何遗留的 seat 指针 grab。

    全部幂等，可在任意时刻安全调用（atexit / 信号退出 / 空闲 watchdog / 启动）：
    - _force_clear_uinput_mods：内核对'本设备未按下的键'的 release 是 no-op，
      不会取消用户经 Synergy/物理键盘按下的真实修饰键。
    - _force_clear_xtest_mods：覆盖 pynput/xdotool 写入 X server 的"幽灵按下"，
      这是唯一能跨进程死亡存活、也是 uinput 清理够不到的卡键层。
    - seat.ungrab：仅释放本客户端持有的 grab，无活跃 grab 时为 no-op。
    """
    _force_clear_uinput_mods()
    _force_clear_xtest_mods()
    _release_seat_grab()


def _release_seat_grab() -> None:
    """解除任何遗留的 seat 指针 grab（仅本客户端持有的 grab，无活跃时 no-op，无害）。"""
    try:
        display = Gdk.Display.get_default()
        if display is not None:
            seat = display.get_default_seat()
            if seat is not None:
                seat.ungrab()
    except Exception:
        pass


def _sd_notify(state: str) -> None:
    """向 systemd 发送状态通知（READY=1 / WATCHDOG=1）。

    纯 socket 实现，无第三方依赖。非 systemd 运行（NOTIFY_SOCKET 未设置）或任何
    异常时静默 no-op —— 手动 ./start.sh 前台运行完全不受影响。
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        import socket
        if addr[0] == "@":  # systemd 抽象命名空间地址
            addr = "\0" + addr[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode("utf-8"), addr)
    except Exception:
        pass


def _idle_safety_watchdog() -> bool:
    """空闲兜底：无录音且无浮窗时，周期性断言无残留修饰键 / 指针 grab。

    修复两类偶发故障并使其在 ~2s 内自愈：
    - 注入中途异常 → Ctrl/Super 卡在按下态 → 经 Synergy 注入的按键全变快捷键；
    - 浮窗 grab 未释放 → 鼠标被圈住。
    录音中或浮窗活跃时跳过（浮窗自己持有 grab，不可越权释放）；
    但录音若超过 VOICE_MAX_SECONDS 视为 release 丢失导致的卡死，强制复位，
    防止 voice_active 永久为 True 把整个自愈系统旁路。

    同时每轮向 systemd 喂狗（WATCHDOG=1）：本回调不再被调度 = GTK 主循环卡死，
    systemd 在 WatchdogSec 后 SIGABRT 重启 —— 这是"进程活着但主循环挂死"唯一兜底。
    """
    _sd_notify("WATCHDOG=1")
    grabber = _hotkey_grabber
    if grabber is not None and getattr(grabber, "voice_active", False):
        started = getattr(grabber, "voice_started_at", 0.0)
        if started and (time.monotonic() - started) > VOICE_MAX_SECONDS:
            print(f"[watchdog] voice_active 超过 {VOICE_MAX_SECONDS:.0f}s, 判定 "
                  "release 丢失, 强制复位", flush=True)
            try:
                grabber.voice_active = False
                grabber.recorder.stop()             # 释放麦克风 + 结束录音状态
                _release_hotkey_modifiers(HOTKEY_VOICE)
            except Exception:
                pass
            # 落到下方执行 panic 清理
        else:
            return True  # 正常录音中，触发键/修饰键可能正被按住，跳过
    if _translation_overlay is not None:
        return True  # 浮窗活跃，其 pointer grab 由浮窗自身生命周期管理
    # 注入进行中（worker/repeat 持有 _inject_lock）则本轮跳过，绝不撕正在进行的粘贴
    if _inject_lock.acquire(blocking=False):
        try:
            # uinput 层 + 指针 grab 无条件清理（对用户真实按键无副作用）
            _force_clear_uinput_mods()
            _release_seat_grab()
            # XTest 层是唯一可能"误伤用户正常长按"的清理，故智能化：
            # 有卡键检测能力(XInput2)时仅在确有卡住的修饰键才清；
            # 无检测能力的抓取器保持原兜底（无条件清）。
            if grabber is not None and hasattr(grabber, "has_stuck_modifiers"):
                if grabber.has_stuck_modifiers():
                    print("[watchdog] 检测到卡住的修饰键, 清理 XTest 层", flush=True)
                    _force_clear_xtest_mods()
            else:
                _force_clear_xtest_mods()
        finally:
            _inject_lock.release()
    return True


def _inject_uinput_paste(is_term: bool) -> bool:
    """用 uinput 合成 Ctrl(+Shift)+V 粘贴，成功返回 True。

    uinput 是独立虚拟设备：按下/松开的是虚拟 Ctrl，绝不触碰用户物理修饰键，
    从根上没有 xdotool --clearmodifiers 的"恢复期重按"卡键。press→syn→release
    →syn 全程配对，finally 幂等清零并补一次 syn，确保 release 帧真正被内核消费
    （否则 release 滞留内核队列，消费者视角按键持续卡住）。
    """
    if not _uinput:
        return False
    from evdev import ecodes
    seq: list[int] = [ecodes.KEY_LEFTCTRL]
    if is_term:
        seq.append(ecodes.KEY_LEFTSHIFT)
    seq.append(ecodes.KEY_V)
    pressed: list[int] = []
    ok = False
    try:
        for code in seq:
            _uinput.write(ecodes.EV_KEY, code, 1)
            pressed.append(code)
        _uinput.syn()
        time.sleep(0.03)
        _uinput.write(ecodes.EV_KEY, ecodes.KEY_V, 0)
        ok = True
    except Exception as exc:
        print(f"  输入: uinput 粘贴失败: {exc}", flush=True)
    finally:
        for code in reversed(pressed):
            try:
                _uinput.write(ecodes.EV_KEY, code, 0)
            except Exception:
                pass
        try:
            _uinput.syn()
        except Exception as exc:
            print(f"  输入: uinput release syn 失败: {exc}", flush=True)
        _force_clear_uinput_mods()
    return ok


def _type_text(text: str) -> str:
    """将文字输入到当前焦点窗口，返回实际采用的输入模式。

    全程持有 _inject_lock（与重复输入/看门狗互斥），并在注入前等待用户物理
    修饰键松开，杜绝并发撕键与"按住 Ctrl 粘贴"的组合误触发 / 重按卡键。

    Tier 1: IBus commit_text —— 输入法框架直接提交（最可靠，无剪贴板副作用）
    Tier 2: uinput —— 内核级独立虚拟设备（不触碰用户物理修饰键，首选粘贴方式）
    Tier 3: xdotool / wtype —— 子进程合成粘贴键（回退，检查返回码）
    Tier 4: pynput —— XTest 合成按键（最后回退）
    """
    global _inject_until
    with _inject_lock:
        _inject_until = time.monotonic() + 3.0  # 注入期间抓取器忽略回流按键
        try:
            time.sleep(0.1)  # 等待焦点稳定

            # ---- Tier 1: IBus 输入法直接提交（不涉及按键合成，无需等修饰键）----
            if _try_ibus_commit(text):
                print(f"  输入: Tier1 IBus 直接提交成功 ({len(text)} 字)", flush=True)
                return "direct"

            # ---- 写入剪贴板 ----
            if not copy_to_clipboard(text):
                raise RuntimeError("剪贴板写入失败")
            # xclip → X11 CLIPBOARD → Mutter 桥接 → Wayland 剪贴板，需要足够延迟
            time.sleep(0.3)
            clip_check = _get_clipboard_text()
            if clip_check != text:
                print(f"  输入: [WARN] 剪贴板验证失败! 期望 {len(text)} 字, 实际 {len(clip_check) if clip_check else 0} 字", flush=True)
                if not copy_to_clipboard(text):
                    raise RuntimeError("剪贴板重试写入失败")
                time.sleep(0.3)
            else:
                print(f"  输入: 剪贴板已写入 ({len(text)} 字) ✓", flush=True)

            is_term = _is_terminal_focused()
            print(f"  输入: IBus 失败, 使用剪贴板粘贴 (终端={is_term}, 会话={_SESSION_TYPE})", flush=True)

            # 注入前等用户物理修饰键真正松开，避免与按住的 Ctrl/Alt 组合成误触发
            _wait_modifiers_released()

            if _SESSION_TYPE == "wayland":
                # Wayland 无法检测焦点窗口类型，始终 Ctrl+Shift+V（终端需要 Shift，
                # 其他应用视为"粘贴纯文本"）
                if _inject_uinput_paste(True):
                    print("  输入: Tier2 uinput Ctrl+Shift+V 已发送", flush=True)
                    return "paste"
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
                # X11: 优先 uinput（独立设备，无 --clearmodifiers 重按竞态）
                if _inject_uinput_paste(is_term):
                    print("  输入: Tier2 uinput 粘贴已发送 (X11)", flush=True)
                    return "paste"
                # X11 回退: xdotool（不再用 --clearmodifiers；注入前已等修饰键松开；查返回码）
                if _xdotool:
                    try:
                        combo = "ctrl+shift+v" if is_term else "ctrl+v"
                        r = subprocess.run(
                            [_xdotool, "key", combo],
                            timeout=3, check=False, capture_output=True,
                        )
                        if r.returncode == 0:
                            print(f"  输入: Tier3 xdotool {combo} 已发送", flush=True)
                            return "paste"
                        print(f"  输入: xdotool 返回码 {r.returncode}, 回退下一级", flush=True)
                    except Exception as exc:
                        print(f"  输入: xdotool 异常 {exc}, 回退下一级", flush=True)

            # ---- Tier 4: pynput 回退（XTest，仅 X11）----
            if _kb is not None:
                pressed_keys: list = [Key.ctrl]
                if is_term:
                    pressed_keys.append(Key.shift)
                held: list = []
                sent_ok = False
                try:
                    for k in pressed_keys:
                        _kb.press(k)
                        held.append(k)
                    _kb.tap("v")
                    sent_ok = True
                except Exception as exc:
                    print(f"  输入: Tier4 pynput 失败: {exc}", flush=True)
                finally:
                    for k in reversed(held):
                        try:
                            _kb.release(k)
                        except Exception:
                            pass
                    _force_clear_pynput_mods()
                if sent_ok:
                    print("  输入: Tier4 pynput 粘贴已发送", flush=True)
                    return "paste"

            print("  输入: [ERROR] 所有输入方式均不可用！", flush=True)
            raise RuntimeError("无法模拟粘贴：xdotool/uinput/pynput 均不可用")
        finally:
            _inject_until = time.monotonic() + 0.2  # 注入尾窗，覆盖回流事件延迟


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
    border-radius: 14px;
    border: 1px solid rgba(100, 140, 255, 0.25);
    min-width: 150px;
    min-height: 60px;
    box-shadow: 0 2px 16px rgba(0, 0, 0, 0.45);
}
.status-dot {
    min-width: 10px;
    min-height: 10px;
    border-radius: 5px;
    background-color: #4ade80;
    margin-right: 6px;
}
.status-recording .status-dot { background-color: #ef4444; }
.status-busy .status-dot { background-color: #f59e0b; }
.status-error .status-dot { background-color: #ef4444; }
.status-success .status-dot { background-color: #22c55e; }
.status-icon {
    color: rgba(180, 200, 230, 0.85);
    font-size: 28px;
    margin-right: 8px;
}
.status-recording .status-icon { color: #fca5a5; }
.status-busy .status-icon { color: #fcd34d; }
.status-error .status-icon { color: #fca5a5; }
.status-success .status-icon { color: #86efac; }
.status-label {
    color: #c0c8e0;
    font-size: 16px;
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

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        hbox.set_valign(Gtk.Align.CENTER)
        self.ebox.add(hbox)

        # 状态圆点
        self.dot = Gtk.DrawingArea()
        self.dot.set_size_request(8, 8)
        self.dot.set_valign(Gtk.Align.CENTER)
        self.dot.get_style_context().add_class("status-dot")
        hbox.pack_start(self.dot, False, False, 0)

        # 麦克风图标
        self.icon_label = Gtk.Label(label="\U0001f3a4")
        self.icon_label.set_valign(Gtk.Align.CENTER)
        self.icon_label.get_style_context().add_class("status-icon")
        hbox.pack_start(self.icon_label, False, False, 0)

        # 状态文字
        self.label = Gtk.Label()
        self.label.get_style_context().add_class("status-label")
        self.label.set_justify(Gtk.Justification.LEFT)
        self.label.set_max_width_chars(12)
        self.label.set_line_wrap(False)
        self.label.set_text("就绪")
        hbox.pack_start(self.label, False, False, 0)

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

        self.set_default_size(150, 60)
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
        if len(text) > 12:
            # 截取关键词。"失败"/"太短" 必须排在"转写"/"录音"之前，否则
            # "转写失败: ..." 会先命中"转写"被错误显示成"转写中"（进行中假象）。
            for kw in ("失败", "太短", "录音", "转写", "已输入", "截图", "排队", "识别", "就绪", "重复"):
                if kw in text:
                    short = kw
                    if kw == "录音":
                        short = "录音中"
                    elif kw == "转写":
                        short = "转写中"
                    break
            else:
                short = text[:10] + "…"
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
        # 右键(button==3) 不再弹"退出"菜单: daemon 是基础设施, 不应被 UI 关闭
        # 真要停 daemon: systemctl --user stop ollama-voice-daemon (或 disable --now)

    def _on_release(self, widget, event):
        if self._dragging:
            self._dragging = False

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


def _quit_daemon(_item=None) -> None:
    # systemctl stop 让 systemd 感知是主动停止, 不触发 Restart=always; 否则 3 秒后自愈
    subprocess.Popen(["systemctl", "--user", "stop", "ollama-voice-daemon"])


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

    # 退出: 调 systemctl --user stop, systemd 收到主动停止信号不会触发 Restart=always
    quit_item = Gtk.MenuItem(label="退出")
    quit_item.connect("activate", _quit_daemon)
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

        self._auto_close_id = 0
        self._max_life_id = 0
        self._pulse_id = 0
        self._text = ""
        self._sel_x, self._sel_y = x, y
        self._sel_w, self._sel_h = w, h

        self._pointer_grabbed = False
        self._outside_check_id = 0
        self._copy_btn = None  # 记录复制按钮，点击时不关闭

        # 窗口属性
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(True)
        self.set_resizable(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.connect("key-press-event", self._on_key_press)
        self.connect("grab-broken-event", self._on_grab_broken)
        # 窗口级别捕获所有点击（包括子控件上的点击）
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self._on_window_click)

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

        self.add(self._box)

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
        # 用 pointer grab 捕获窗口外点击，而不是依赖焦点状态推断。
        GLib.idle_add(self._grab_pointer)
        # 轮询检测窗口外点击（grab 在 GTK3 下不一定触发 button-press-event）
        self._outside_check_id = GLib.timeout_add(100, self._check_outside_click)
        # 硬性最大存活上限：即使加载/结果流程异常卡死（如 API 无响应），
        # 也强制销毁，经 _on_destroy 释放 pointer grab，杜绝鼠标被永久圈住。
        self._max_life_id = GLib.timeout_add(30000, self._auto_close)

    def _pulse(self) -> bool:
        self._progress.pulse()
        return True

    def _on_key_press(self, widget, event) -> bool:
        """Esc 键关闭覆盖层。"""
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _grab_pointer(self) -> bool:
        """抓取 pointer，把窗口外 ButtonPress 也路由到当前窗口。"""
        gdk_win = self.get_window()
        if gdk_win is None:
            return False
        display = gdk_win.get_display()
        seat = display.get_default_seat()
        if seat is None:
            return False
        status = seat.grab(
            gdk_win,
            Gdk.SeatCapabilities.ALL_POINTING,
            True,
            None,
            None,
            None,
        )
        self._pointer_grabbed = (status == Gdk.GrabStatus.SUCCESS)
        if not self._pointer_grabbed:
            print(f"  TranslationOverlay: pointer grab failed ({int(status)})", file=sys.stderr)
        return False

    def _on_grab_broken(self, widget, event) -> bool:
        self._pointer_grabbed = False
        return False

    def _check_outside_click(self) -> bool:
        """轮询检测：指针在窗口外且有鼠标按键按下时关闭（不依赖焦点状态）。"""
        gdk_win = self.get_window()
        if gdk_win is None:
            return False
        seat = Gdk.Display.get_default().get_default_seat()
        pointer = seat.get_pointer()
        if pointer is None:
            return True
        # 获取指针根坐标
        _, rpx, rpy = pointer.get_position()
        wx, wy = self.get_position()
        ww, wh = self.get_size()
        if not (wx <= rpx <= wx + ww and wy <= rpy <= wy + wh):
            # 指针在窗口外，检查是否有鼠标按键按下
            _, _, _, mask = gdk_win.get_device_position(pointer)
            button_mask = (
                Gdk.ModifierType.BUTTON1_MASK
                | Gdk.ModifierType.BUTTON2_MASK
                | Gdk.ModifierType.BUTTON3_MASK
            )
            if mask & button_mask:
                self.destroy()
                return False
        return True

    def _window_bounds(self) -> tuple[int, int, int, int]:
        wx, wy = self.get_position()
        ww, wh = self.get_size()
        return wx, wy, ww, wh

    def _on_window_click(self, widget, event) -> bool:
        """窗口级点击：复制按钮区域放行，其余位置关闭。"""
        wx, wy, ww, wh = self._window_bounds()
        local_x = event.x_root - wx
        local_y = event.y_root - wy
        if not (0 <= local_x < ww and 0 <= local_y < wh):
            self.destroy()
            return True
        if self._copy_btn and self._copy_btn.get_mapped():
            btn_alloc = self._copy_btn.get_allocation()
            # translate_coordinates 把按钮左上角转换到窗口坐标
            ok, bx, by = self._copy_btn.translate_coordinates(self, 0, 0)
            if ok and bx <= local_x < bx + btn_alloc.width and by <= local_y < by + btn_alloc.height:
                return False  # 让按钮自己处理
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

        # 10s 自动关闭 (浮窗作为通知型 UI 应主动消亡, 不依赖用户操作)
        self._auto_close_id = GLib.timeout_add(10000, self._auto_close)

    def _on_copy(self, btn) -> None:
        copy_to_clipboard(self._text)
        btn.set_label("已复制 \u2713")
        btn.set_sensitive(False)

    def _auto_close(self) -> bool:
        self.destroy()
        return False

    def _on_destroy(self, *args) -> None:
        global _translation_overlay
        # 最优先：释放 pointer grab（鼠标被圈住的唯一来源），且无论后续是否异常，
        # 都在 finally 里清空全局引用 —— 否则本函数半途抛错会跳过清空，watchdog 因
        # _translation_overlay 非空永久跳过（第 697 行），鼠标被永久锁死。
        try:
            if self._pointer_grabbed:
                display = Gdk.Display.get_default()
                if display is not None:
                    seat = display.get_default_seat()
                    if seat is not None:
                        seat.ungrab()
        except Exception:
            pass
        finally:
            self._pointer_grabbed = False
            _translation_overlay = None
        # 各定时器逐个安全移除（source_remove 对已失效 id 可能告警，各自 guard）
        for _attr in ("_pulse_id", "_auto_close_id", "_max_life_id", "_outside_check_id"):
            _sid = getattr(self, _attr, 0)
            if _sid:
                try:
                    GLib.source_remove(_sid)
                except Exception:
                    pass
                setattr(self, _attr, 0)


def _close_translation_overlay() -> bool:
    """关闭当前活跃的翻译浮窗 (任意键路径触发, GLib 主线程安全).

    用于 XInputHotkeyGrabber 检测到非快捷键按键时主动关闭浮窗,
    弥补"焦点 ESC + 鼠标外点"双路径在键盘工作流下的盲区.
    """
    global _translation_overlay
    if _translation_overlay is not None:
        try:
            _translation_overlay.destroy()
        except Exception:
            pass
        _translation_overlay = None
    return False  # 单次 idle_add 执行


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
            # 记录说话瞬间的焦点窗口，供转写完成后注入前比对，防错窗粘贴
            global _record_focus_xid
            _record_focus_xid = _get_focus_xid()
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

        if self._stream is not None:
            # 流关闭异常（设备热拔 / PipeWire 重启）绝不能向上抛：否则调用方
            # _do_release 里其后的 _release_hotkey_modifiers 被跳过 = 修饰键卡住，
            # 且麦克风流句柄泄漏。异常吞掉但打印，finally 保证句柄一定置空。
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                print(f"[VoiceRecorder] 关闭音频流异常: {exc}", file=sys.stderr)
            finally:
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
                    verify=API_VERIFY_TLS,
                )
                if resp.status_code != 200:
                    try:
                        detail = resp.json().get("detail", resp.text[:200])
                    except Exception:
                        detail = resp.text[:200]
                    raise RuntimeError(detail)

            result = resp.json()
            final = result.get("final_text", "")
            print(f"  转写: 结果 ({len(final)} 字, status={resp.status_code})", flush=True)
            if final:
                # 末尾自动加句号（根据语言选择中/英文标点）
                if final[-1] not in "。！？.!?…；;:：,，、":
                    final += "。" if WHISPER_LANGUAGE in ("zh", "ja", "ko") else "."
                global _last_result
                _last_result = final
                # 注入前比对焦点：说话时的窗口若已切走，则跳过粘贴，避免文字
                # 打到错误窗口（IBus/剪贴板粘贴均无差别打到当前焦点）。
                # 两端任一未知(=0, 如 Wayland)则不拦，宁可不拦也不误拦。
                expected_xid = _record_focus_xid
                current_xid = _get_focus_xid()
                if expected_xid and current_xid and expected_xid != current_xid:
                    print(f"  输入: 焦点已切换 {expected_xid}→{current_xid}, 跳过粘贴（防错窗）", flush=True)
                    set_status("焦点已切换，未粘贴（可用重复输入键重发）", "error")
                else:
                    with _inject_lock:
                        old_clipboard = _get_clipboard_text()
                        input_mode = _type_text(final)
                        time.sleep(0.3)
                        # 仅在剪贴板仍是本次结果时恢复，避免覆盖用户新复制的内容
                        current_clip = _get_clipboard_text()
                        if current_clip == final:
                            copy_to_clipboard(old_clipboard)
                    set_status(_input_status_text(input_mode), "success")
            else:
                set_status("没有识别到内容", "error")
        except Exception as exc:
            traceback.print_exc()
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
                        and not _screenshot_active()
                        and not (_hotkey_grabber and _hotkey_grabber.voice_active)
                        else None,
                        False,
                    )[-1],
                )


# ---------------------------------------------------------------------------
# 截图翻译（maim 选区 → 原位覆盖层显示加载 → OCR+翻译 → 覆盖层显示结果）
# ---------------------------------------------------------------------------

_screenshot_proc: subprocess.Popen | None = None
_screenshot_lock = threading.Lock()


def screenshot_translate() -> None:
    """截图翻译：框选截图 → tesseract OCR → 翻译 → 选区原位覆盖层。

    设计: 不 hold 后续触发, 上次 maim 还在跑则 terminate 它, 立即启新一次.
    """
    global _screenshot_proc

    # 取消上次仍在跑的截图进程 (第一性原理: 快捷键触发应独立, 不应被前次 hold)
    with _screenshot_lock:
        prev = _screenshot_proc
    if prev is not None and prev.poll() is None:
        try:
            prev.terminate()
        except Exception:
            pass

    tool = detect_screenshot_tool()
    if not tool:
        set_status("未找到截图工具 (maim/grim+slurp/gnome-screenshot)", "error")
        _reset_status_later()
        return

    # 在主线程立即启动截图进程，消除线程调度延迟
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name

    if tool == "x11-maim":
        cmd = ["maim", "-s", tmp_path]
    elif tool == "wayland-grim":
        cmd = ["bash", "-c", f'grim -g "$(slurp)" {tmp_path}']
    else:  # gnome
        cmd = ["gnome-screenshot", "-a", "-f", tmp_path]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with _screenshot_lock:
        _screenshot_proc = proc
    set_status("截图选择中...", "busy")
    threading.Thread(
        target=_do_screenshot_translate, args=(proc, tmp_path), daemon=True
    ).start()


def _do_screenshot_translate(proc: subprocess.Popen, tmp_path: str) -> None:
    """后台执行截图翻译流程（maim 进程已在主线程启动）。"""
    global _screenshot_proc
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
                timeout=httpx.Timeout(5.0, connect=5.0, read=120.0),
                verify=API_VERIFY_TLS,
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
        traceback.print_exc()
        set_status(f"截图失败: {str(exc)[:60]}", "error")
        GLib.idle_add(_update_overlay_result, f"失败: {str(exc)[:40]}", True)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        with _screenshot_lock:
            # 仅清自己启动的那个 proc 引用; 若已被新触发覆盖则保留新的
            if _screenshot_proc is proc:
                _screenshot_proc = None
        _reset_status_later(5000)


def _do_repeat_common() -> str:
    """执行重复输入的公共逻辑，返回输入模式。"""
    with _inject_lock:
        old_clipboard = _get_clipboard_text()
        input_mode = _type_text(_last_result)
        time.sleep(0.3)
        current = _get_clipboard_text()
        if current == _last_result:
            copy_to_clipboard(old_clipboard)
    return input_mode


def _inject_web_text(text: str) -> str:
    """把来自网页(手机/其他电脑)的转写文本注入到本机当前焦点窗口，返回输入模式。

    复用语音注入链路(_type_text)与全局注入锁，与本地录音/重复输入完全互斥。
    与本地录音不同：文本源自远端，用户此刻大概率没按任何修饰键，_type_text 内已
    有等修饰键松开的保护，直接复用即可；注入完成后写入 _last_result 供重复键复用。
    """
    global _last_result
    if not text:
        return ""
    _last_result = text
    with _inject_lock:
        old_clipboard = _get_clipboard_text()
        input_mode = _type_text(text)
        time.sleep(0.3)
        current = _get_clipboard_text()
        if current == text:
            copy_to_clipboard(old_clipboard)
    # set_status 内部已用 GLib.idle_add 转主线程，可从本后台线程直接调
    set_status(f"网页输入: {_input_status_text(input_mode)}", "success")
    _reset_status_later()
    return input_mode


def _web_input_report(job_id: str, ok: bool, detail: str = "") -> None:
    """把注入结果回传给 app.py，供手机端 /status 实时同步电脑侧进度。"""
    if not job_id:
        return
    try:
        httpx.post(
            f"{API_BASE}/api/web_input/report",
            data={"job_id": job_id, "ok": str(ok).lower(), "detail": detail},
            timeout=5,
            verify=API_VERIFY_TLS,
        )
    except Exception as exc:
        print(f"  网页录入: 回传状态失败 {exc}", flush=True)


def _web_input_poll_loop() -> None:
    """后台线程：长轮询 app.py 的待注入队列，取到就注入本机焦点窗口。

    单线程顺序消费，天然与本地录音共享 _inject_lock 串行，不会并发撕键。
    网络异常时退避重试，不影响本地语音/快捷键功能。
    """
    print("  网页录入: 长轮询线程已启动", flush=True)
    while True:
        try:
            resp = httpx.get(
                f"{API_BASE}/api/web_input/poll",
                params={"wait": 25.0},
                timeout=httpx.Timeout(5.0, read=30.0),
                verify=API_VERIFY_TLS,
            )
            if resp.status_code != 200:
                time.sleep(3)
                continue
            data = resp.json() or {}
            text = data.get("text")
            job_id = data.get("job_id", "")
            if text:
                print(f"  网页录入: 收到文本 ({len(text)} 字), 注入焦点窗口", flush=True)
                # 直接在本后台线程注入（_type_text 本就设计为在 worker 线程运行，
                # 含 sleep+子进程；绝不能丢到 GTK 主线程执行，否则冻结主循环+看门狗）。
                try:
                    mode = _inject_web_text(text)
                    _web_input_report(job_id, True, _input_status_text(mode))
                except Exception as exc:
                    print(f"  网页录入: 注入失败 {exc}", flush=True)
                    _web_input_report(job_id, False, str(exc)[:80])
        except httpx.TimeoutException:
            continue  # 长轮询正常空转
        except Exception as exc:
            print(f"  网页录入: 轮询异常 {exc}, 3s 后重试", flush=True)
            time.sleep(3)


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
        self.voice_started_at = 0.0  # 录音开始时刻，供看门狗判定 release 丢失卡死
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
                    self.voice_started_at = time.monotonic()
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
            _release_hotkey_modifiers(HOTKEY_VOICE)
            # 此处不做 ungrab/grab, 保持 XGrabKey 持续活跃
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
            input_mode = _do_repeat_common()
            set_status(_input_status_text(input_mode, repeated=True), "success")
        except Exception as exc:
            traceback.print_exc()
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
# XInput2 RawKey 全局快捷键 (X server 客户端层 raw event stream)
#
# 与 XGrabKey 的差异: XInput2 raw 监听设备层事件, 不依赖 X server grab 链,
# 能拦截 XTest / XSendEvent 注入按键 (KVM / Synergy / Logitech Flow 场景).
# 与 evdev 的差异: 走 X server, 不需要 /dev/input/event* 设备节点.
# ---------------------------------------------------------------------------

# 本进程 uinput 设备名（main 里创建，XI2 raw 的 sourceid 过滤用同一名字）
_UINPUT_DEVICE_NAME = "ollama-voice-input"


def _xi_source_ids_by_name(dpy, name: str) -> set[int]:
    """查出 XInput2 中指定设备名的全部 deviceid（slave sourceid）。一行调用。"""
    ids: set[int] = set()
    try:
        reply = dpy.xinput_query_device(xi_ext.AllDevices)
    except Exception:
        return ids
    for info in getattr(reply, "devices", None) or ():
        if getattr(info, "name", None) != name:
            continue
        did = getattr(info, "deviceid", None)
        if did:
            ids.add(int(did))
    return ids


# RawKey 事件的 wire format (xXIRawEvent in XI2proto.h):
# deviceid(u16) time(u32) detail(u32=keycode) sourceid(u16) valuators_len(u16) flags(u32) pad(u32)
_XI_RAW_EVENT_DATA = xrq.Struct(
    xrq.Card16('deviceid'),
    xrq.Card32('time'),
    xrq.Card32('detail'),
    xrq.Card16('sourceid'),
    xrq.Card16('valuators_len'),
    xrq.Card32('flags'),
    xrq.Pad(4),
)


class XInputHotkeyGrabber:
    """XInput2 RawKey 全局快捷键管理, 通过 GLib.io_add_watch 集成到 GTK 事件循环.

    raw 事件没有 modifier mask, 自己用 keycode set 维护按下状态.
    与 HotkeyGrabber 接口契约一致: voice_active / start / cleanup / grab / ungrab.
    """

    def __init__(self, recorder: VoiceRecorder) -> None:
        self.recorder = recorder
        self.voice_active = False
        self.voice_started_at = 0.0  # 录音开始时刻，供看门狗判定 release 丢失卡死
        self._pending_release_id = 0
        self._pressed: set[int] = set()
        self._ignore_source_ids: set[int] = set()

        self.dpy = xdisplay.Display()
        self.dpy.set_error_handler(self._ignore_error)
        if not self.dpy.has_extension('XInputExtension'):
            self.dpy.close()
            raise RuntimeError("X server 未启用 XInputExtension")

        self._major = self.dpy.display.get_extension_major(xi_ext.extname)
        qv = self.dpy.xinput_query_version()
        if qv.major_version < 2:
            self.dpy.close()
            raise RuntimeError(
                f"XInput 版本过低 ({qv.major_version}.{qv.minor_version}), 需要 >= 2.0"
            )

        # 注册 RawKey event-data parser (一次性, 注册后由 display 持有)
        try:
            self.dpy.ge_add_event_data(self._major, xi_ext.RawKeyPress, _XI_RAW_EVENT_DATA)
            self.dpy.ge_add_event_data(self._major, xi_ext.RawKeyRelease, _XI_RAW_EVENT_DATA)
        except Exception as exc:
            self.dpy.close()
            raise RuntimeError(f"无法注册 XInput2 RawKey parser: {exc}") from exc

        self.root = self.dpy.screen().root

        # 智能卡键检测：所有修饰键 keycode 集 + 各自按下起始时刻 + 最近按键活动时刻
        self._mod_kcs: set[int] = set()
        for _nm in ("Control_L", "Control_R", "Shift_L", "Shift_R",
                    "Alt_L", "Alt_R", "Super_L", "Super_R", "Meta_L", "Meta_R"):
            _ks = XK.string_to_keysym(_nm)
            if _ks:
                _kc = self.dpy.keysym_to_keycode(_ks)
                if _kc:
                    self._mod_kcs.add(_kc)
        self._mod_down_since: dict[int, float] = {}
        self._last_key_activity = time.monotonic()
        self._last_stuck_check = 0.0  # 打字时卡键检测的节流时间戳

        # 解析三个快捷键为 (trigger_keycode, [modifier_keycode_set, ...])
        self._voice_combo = self._compile_hotkey(HOTKEY_VOICE)
        self._screen_combo = self._compile_hotkey(HOTKEY_SCREENSHOT)
        self._repeat_combo = self._compile_hotkey(HOTKEY_REPEAT)
        print(
            f"  XInput2: 语音 {HOTKEY_VOICE} "
            f"→ trigger=kc{self._voice_combo['trigger']} mods={self._voice_combo['modifiers']}"
        )
        print(
            f"  XInput2: 截图 {HOTKEY_SCREENSHOT} "
            f"→ trigger=kc{self._screen_combo['trigger']} mods={self._screen_combo['modifiers']}"
        )
        print(
            f"  XInput2: 重复 {HOTKEY_REPEAT} "
            f"→ trigger=kc{self._repeat_combo['trigger']} mods={self._repeat_combo['modifiers']}"
        )

    def _compile_hotkey(self, hotkey_str: str) -> dict:
        """解析快捷键字符串 → {'trigger': keycode, 'modifiers': [set, ...]} (X11 keycode)."""
        mask, keysym = _parse_hotkey(hotkey_str)
        trigger_kc = self.dpy.keysym_to_keycode(keysym)
        if not trigger_kc:
            raise ValueError(f"无法解析快捷键 {hotkey_str} 的触发键 keysym=0x{keysym:x}")
        modifiers = []
        if mask & X.ControlMask:
            modifiers.append(self._kc_set([XK.XK_Control_L, XK.XK_Control_R]))
        if mask & X.ShiftMask:
            modifiers.append(self._kc_set([XK.XK_Shift_L, XK.XK_Shift_R]))
        if mask & X.Mod1Mask:
            modifiers.append(
                self._kc_set([XK.XK_Alt_L, XK.XK_Alt_R, XK.XK_Meta_L, XK.XK_Meta_R])
            )
        if mask & X.Mod4Mask:
            modifiers.append(self._kc_set([XK.XK_Super_L, XK.XK_Super_R]))
        return {'trigger': trigger_kc, 'modifiers': modifiers}

    def _kc_set(self, keysyms: list[int]) -> set[int]:
        out: set[int] = set()
        for ks in keysyms:
            kc = self.dpy.keysym_to_keycode(ks)
            if kc:
                out.add(kc)
        return out

    @staticmethod
    def _ignore_error(err, *args):
        pass

    def grab(self) -> None:
        pass  # XInput2 raw 是被动监听, 不需要 grab

    def ungrab(self) -> None:
        pass

    def start(self) -> None:
        mask = xi_ext.RawKeyPressMask | xi_ext.RawKeyReleaseMask
        self.root.xinput_select_events([(xi_ext.AllMasterDevices, mask)])
        self.dpy.flush()
        self._refresh_ignore_sources()
        GLib.io_add_watch(self.dpy.fileno(), GLib.IO_IN, self._on_xlib_event)

    def _refresh_ignore_sources(self) -> None:
        """只滤自家 uinput。不能滤 XTEST：Synergy 的真实键盘走那条设备。"""
        self._ignore_source_ids = _xi_source_ids_by_name(self.dpy, _UINPUT_DEVICE_NAME)

    def _is_injected_source(self, sourceid: int) -> bool:
        return bool(sourceid) and sourceid in self._ignore_source_ids

    def _on_xlib_event(self, fd, condition) -> bool:
        try:
            self.dpy.sync()
        except Exception as e:
            print(f"XInput2 sync error: {e}", file=sys.stderr)
            return True
        while self.dpy.pending_events() > 0:
            try:
                e = self.dpy.next_event()
                if getattr(e, 'type', None) == X.MappingNotify:
                    self._refresh_ignore_sources()
                    continue
                if getattr(e, 'extension', None) != self._major:
                    continue
                evt = getattr(e, 'evtype', None)
                if evt not in (xi_ext.RawKeyPress, xi_ext.RawKeyRelease):
                    continue
                if self._is_injected_source(getattr(e.data, 'sourceid', 0)):
                    continue
                if evt == xi_ext.RawKeyPress:
                    self._handle_press(e.data.detail)
                else:
                    self._handle_release(e.data.detail)
            except Exception as e:
                print(f"XInput2 event error: {e}", file=sys.stderr)
        return True

    def _handle_press(self, keycode: int) -> None:
        # 找不到 uinput 设备时，时间窗仍吞 press，避免回流自触发。
        # 正常路径按 sourceid 过滤，不再吞用户真实按键。
        if not self._ignore_source_ids and time.monotonic() < _inject_until:
            return
        self._last_key_activity = time.monotonic()  # 记录真实按键活动(供卡键检测判空闲)
        if keycode in self._mod_kcs:
            # 记录修饰键按下时刻(Synergy 的 down 可靠, 丢的是 up), 供卡键"持续时长"判定
            self._mod_down_since.setdefault(keycode, self._last_key_activity)
        elif (not self.voice_active
                and self._last_key_activity - self._last_stuck_check > 0.25):
            # 思路②事件驱动自愈：打字(按非修饰键)时若某修饰键已卡住(持续按下超阈值)，
            # 立即清 —— 比 2s 看门狗更快治"边打字边卡"；0.25s 节流避免自动重复刷 X。
            self._last_stuck_check = self._last_key_activity
            if self._has_lingering_modifier(STUCK_MOD_KEYPRESS_SECONDS):
                # 与 _match_combo 同路径：清 XTest + 剔除 raw 账本中的幽灵修饰键
                lingering = self._lingering_modifiers()
                ghosts = {
                    kc: h for kc, h in lingering.items()
                    if h > STUCK_MOD_KEYPRESS_SECONDS
                }
                print(
                    f"[stuck] 打字时检测到卡住的修饰键 {list(ghosts)}, 立即清理",
                    flush=True,
                )
                _force_clear_xtest_mods()
                for kc in ghosts:
                    self._pressed.discard(kc)
                    self._mod_down_since.pop(kc, None)
        is_repeat = keycode in self._pressed
        self._pressed.add(keycode)
        # 录音中按住触发键的自动重复 → 取消挂起的 release 定时器
        if keycode == self._voice_combo['trigger'] and self.voice_active:
            if self._pending_release_id:
                GLib.source_remove(self._pending_release_id)
                self._pending_release_id = 0
            return
        if is_repeat:
            return
        # 任意按键 (含修饰键单独按下) → 关活跃浮窗, 不依赖焦点
        if _translation_overlay is not None:
            GLib.idle_add(_close_translation_overlay)
        if self._match_combo(self._voice_combo, keycode):
            self._on_voice_press()
        elif self._match_combo(self._screen_combo, keycode):
            GLib.idle_add(screenshot_translate)
        elif self._match_combo(self._repeat_combo, keycode):
            self._on_repeat()

    def _handle_release(self, keycode: int) -> None:
        if not self._ignore_source_ids and time.monotonic() < _inject_until:
            # 回退窗：注入键(Ctrl/V)仍忽略；热键 trigger 的抬起必须出账，否则录音卡死
            triggers = {
                self._voice_combo['trigger'],
                self._screen_combo['trigger'],
                self._repeat_combo['trigger'],
            }
            if keycode not in self._pressed or keycode not in triggers:
                return
        self._last_key_activity = time.monotonic()
        self._mod_down_since.pop(keycode, None)  # 修饰键抬起 → 从卡键账本移除
        self._pressed.discard(keycode)
        # 思路①：录音中松开热键组合的"任意一个键"(触发键或其任一修饰键)即视为结束，
        # 停录 + 清整组修饰键 —— 覆盖 Synergy 丢掉组合里某一个键 release 的情况。
        if self.voice_active and self._is_voice_combo_key(keycode):
            if self._pending_release_id:
                GLib.source_remove(self._pending_release_id)
            self._pending_release_id = GLib.timeout_add(50, self._do_release)

    def _is_voice_combo_key(self, keycode: int) -> bool:
        """keycode 是否属于语音热键组合（触发键或其任一修饰键）。"""
        if keycode == self._voice_combo['trigger']:
            return True
        for mod_set in self._voice_combo['modifiers']:
            if keycode in mod_set:
                return True
        return False

    def _lingering_modifiers(self) -> dict[int, float]:
        """返回当前确实按下的修饰键 → 已持续按下秒数。

        query_keymap 为权威源；顺带对账 _mod_down_since（已抬起的移除、漏记的补记），
        因此不会因事件丢失而错乱。供看门狗与打字即时检测复用。
        """
        now = time.monotonic()
        try:
            km = self.dpy.query_keymap()
        except Exception:
            return {}
        down_now = {kc for kc in self._mod_kcs if km[kc >> 3] & (1 << (kc & 7))}
        for kc in list(self._mod_down_since):
            if kc not in down_now:
                self._mod_down_since.pop(kc, None)
        for kc in down_now:
            self._mod_down_since.setdefault(kc, now)
        return {kc: now - self._mod_down_since[kc] for kc in down_now}

    def _has_lingering_modifier(self, min_held: float) -> bool:
        """是否有修饰键确实按下且持续超过 min_held 秒（打字即时检测用，不看空闲）。"""
        return any(h > min_held for h in self._lingering_modifiers().values())

    def has_stuck_modifiers(self) -> bool:
        """看门狗用：修饰键确实按下，且 持续>HARD 或 (持续>SOFT 且 键盘空闲>IDLE)。

        绝不误伤正常长按：长按期间要么持续有按键活动(空闲小)、要么很快松开。
        """
        idle = time.monotonic() - self._last_key_activity
        for held in self._lingering_modifiers().values():
            if held > STUCK_MOD_HARD_SECONDS or (
                held > STUCK_MOD_SOFT_SECONDS and idle > STUCK_MOD_IDLE_SECONDS
            ):
                return True
        return False

    def _match_combo(self, combo: dict, pressed_kc: int | None = None) -> bool:
        """只有刚才按下的键是 trigger 才可能命中；modifier 用 query_keymap。

        禁止用 _pressed 里的陈 trigger 去配后续任意键——那是「松开后还在触发」的根因。
        修饰键位图 down 仍是权威源；幽灵卡键（持续 > STUCK_MOD_MATCH_SECONDS）不计入。
        """
        if pressed_kc is not None and pressed_kc != combo['trigger']:
            return False
        if combo['trigger'] not in self._pressed:
            return False
        if not combo['modifiers']:
            return True
        lingering = self._lingering_modifiers()  # kc → 已持续按下秒数（仅当前 down）
        saw_ghost = False
        for mod_set in combo['modifiers']:
            live = False
            for kc in mod_set:
                held = lingering.get(kc)
                if held is None:
                    continue  # 位图未按下
                if held > STUCK_MOD_MATCH_SECONDS:
                    saw_ghost = True
                    continue  # 幽灵：不计入命中
                live = True
                break
            if not live:
                if saw_ghost:
                    self._reject_ghost_modifiers(lingering)
                return False
        return True

    def _reject_ghost_modifiers(self, lingering: dict[int, float] | None = None) -> None:
        """匹配因幽灵修饰键失败时：清 XTest，并从 raw 账本剔除已超龄的修饰键。"""
        if lingering is None:
            lingering = self._lingering_modifiers()
        ghosts = [kc for kc, held in lingering.items() if held > STUCK_MOD_MATCH_SECONDS]
        if not ghosts:
            return
        print(
            f"[stuck] 热键匹配忽略幽灵修饰键 {ghosts}, 清理 XTest 层",
            flush=True,
        )
        _force_clear_xtest_mods()
        for kc in ghosts:
            self._pressed.discard(kc)
            self._mod_down_since.pop(kc, None)

    def _on_voice_press(self) -> None:
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
        if not self.voice_active:
            self.voice_active = True
            self.voice_started_at = time.monotonic()
            if not self.recorder.start():
                self.voice_active = False

    def _do_release(self) -> bool:
        self._pending_release_id = 0
        if self.voice_active:
            self.voice_active = False
            self.recorder.stop()
            _release_hotkey_modifiers(HOTKEY_VOICE)
        return False

    def _on_repeat(self) -> None:
        if not _last_result:
            set_status("还没有识别记录", "error")
            _reset_status_later(2000)
            return
        set_status("重复输入中...", "busy")
        threading.Thread(target=self._do_repeat, daemon=True).start()

    def _do_repeat(self) -> None:
        try:
            input_mode = _do_repeat_common()
            set_status(_input_status_text(input_mode, repeated=True), "success")
        except Exception as exc:
            traceback.print_exc()
            set_status(f"重复输入失败: {str(exc)[:40]}", "error")
        _reset_status_later()

    def cleanup(self) -> None:
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
        try:
            self.dpy.close()
        except Exception:
            pass


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
        self.voice_started_at = 0.0  # 录音开始时刻，供看门狗判定 release 丢失卡死
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
                if dev.name == _UINPUT_DEVICE_NAME:
                    dev.close()
                    continue
                if self._is_real_keyboard(dev.capabilities()):
                    self._devices.append(dev)
                    print(f"  evdev: 监控键盘 {dev.name} ({dev.path})")
                else:
                    dev.close()
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

    @staticmethod
    def _is_real_keyboard(caps) -> bool:
        """判定一个 evdev 设备是否为真键盘 (排除游戏鼠标宏键、特殊功能键等).

        条件: 含 EV_KEY, 不含鼠标按键 BTN_LEFT, 至少有 5 个字母键 + 含 SPACE.
        """
        from evdev import ecodes
        if ecodes.EV_KEY not in caps:
            return False
        keys = caps[ecodes.EV_KEY]
        if ecodes.BTN_LEFT in keys:
            return False  # 鼠标 (游戏鼠标的宏键也声明 KEY_A..KEY_Z, 必须靠 BTN_LEFT 排除)
        if ecodes.KEY_SPACE not in keys:
            return False
        letter_codes = [getattr(ecodes, f"KEY_{c}") for c in "ABCDEFGHIJKLM"]
        return sum(1 for c in letter_codes if c in keys) >= 5

    def _rescan_devices(self) -> None:
        """重新扫描键盘设备（USB 热插拔恢复）。"""
        from evdev import InputDevice, ecodes, list_devices

        existing_paths = {d.path for d in self._devices}
        for path in list_devices():
            if path in existing_paths:
                continue
            try:
                dev = InputDevice(path)
                if dev.name == _UINPUT_DEVICE_NAME:
                    dev.close()
                    continue
                if self._is_real_keyboard(dev.capabilities()):
                    self._devices.append(dev)
                    print(f"  evdev: 重新发现键盘 {dev.name} ({dev.path})", flush=True)
                else:
                    dev.close()
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

        rescan_interval = 30.0  # 秒, KVM 切换/USB 热插拔的周期重扫节奏
        last_rescan = time.time()
        while self._running:
            now = time.time()
            if now - last_rescan >= rescan_interval:
                self._rescan_devices()
                last_rescan = now

            fds = {}
            for dev in list(self._devices):
                try:
                    fds[dev.fd] = dev
                except Exception as e:
                    print(f"  evdev: [ERROR] 获取fd失败 {dev.name}: {e}", flush=True)

            if not fds:
                print("  evdev: [WARN] 无可用设备, 尝试重新扫描...", flush=True)
                self._rescan_devices()
                last_rescan = time.time()
                time.sleep(3)
                continue

            try:
                r, _, _ = select.select(list(fds.keys()), [], [], 1.0)
            except Exception as e:
                # select 抛异常通常是某个 fd 变坏（EBADF：设备被拔/权限丢失）。
                # 原来只 sleep+continue 会 10Hz 空转刷屏且热键永久失效——这里直接
                # 关闭并丢弃全部当前设备、清残留按键，再重扫重建，让热键自愈。
                print(f"  evdev: [ERROR] select 异常: {type(e).__name__}: {e}, 重建设备", flush=True)
                for d in list(self._devices):
                    try:
                        d.close()
                    except Exception:
                        pass
                self._devices.clear()
                self._pressed_keys.clear()
                self._rescan_devices()
                last_rescan = time.time()
                time.sleep(0.5)
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
                        dev.close()  # 补 close，否则 fd 泄漏（KVM 频繁切换会 EMFILE 拖垮整进程）
                    except Exception:
                        pass
                    try:
                        self._devices.remove(dev)
                    except ValueError:
                        pass
                    # 被拔设备可能有未收到 release 的残留按键 → 清账防幻触发录音
                    self._pressed_keys.clear()
                except Exception as e:
                    print(f"  evdev: [ERROR] 事件读取异常: {type(e).__name__}: {e}", flush=True)

    def _handle_key_event(self, code: int, value: int) -> None:
        """处理按键事件。value: 0=释放, 1=按下, 2=自动重复"""
        if value == 1:  # 按下
            self._pressed_keys.add(code)
            self._check_press(code)
        elif value == 0:  # 释放
            self._pressed_keys.discard(code)
            # 只按静态的 trigger keycode 判定，不在监控线程读 voice_active：
            # 快速点按时 press 的 voice_active=True 还排在主线程 idle 队列里未执行，
            # 若此处用 voice_active 门控会丢掉这次 release → 录音永不停止。
            # 统一交主线程 _do_release（在 press 的 idle 之后执行）按真实状态决定。
            if code == self._voice_combo["trigger"]:
                print("  evdev: 语音触发键释放, 调度停止", flush=True)
                GLib.idle_add(self._schedule_release)
        # value == 2 (自动重复) → 忽略

    def _match_combo(self, combo: dict, pressed_kc: int | None = None) -> bool:
        """只有刚才按下的键是 trigger 才可能命中，避免陈 trigger 配后续任意键。"""
        if pressed_kc is not None and pressed_kc != combo["trigger"]:
            return False
        if combo["trigger"] not in self._pressed_keys:
            return False
        for mod_keys in combo["modifiers"]:
            if not (mod_keys & self._pressed_keys):
                return False
        return True

    def _check_press(self, pressed_kc: int) -> None:
        if self._match_combo(self._voice_combo, pressed_kc):
            print("  evdev: ★ 语音快捷键匹配！调度录音", flush=True)
            GLib.idle_add(self._on_voice_press)
        elif self._match_combo(self._screen_combo, pressed_kc):
            print("  evdev: ★ 截图快捷键匹配！调度截图", flush=True)
            GLib.idle_add(screenshot_translate)
        elif self._match_combo(self._repeat_combo, pressed_kc):
            print("  evdev: ★ 重复快捷键匹配！调度重复", flush=True)
            GLib.idle_add(self._on_repeat)

    def _on_voice_press(self) -> bool:
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
        if not self.voice_active:
            self.voice_active = True
            self.voice_started_at = time.monotonic()
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
            _release_hotkey_modifiers(HOTKEY_VOICE)
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
            input_mode = _do_repeat_common()
            set_status(_input_status_text(input_mode, repeated=True), "success")
        except Exception as exc:
            traceback.print_exc()
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
        resp = httpx.get(f"{API_BASE}/api/setup/check", timeout=3, verify=API_VERIFY_TLS)
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

    # 启动即清残留：上一代进程若崩溃/被 SIGKILL，其 XTest 层按下的修饰键会残留在
    # X server 核心键盘状态里跨进程存活（uinput 层由内核随 fd 关闭自动补 release，
    # 无需处理）。在注册任何抓取器/注入设备之前先幂等清一次，卡键不再穿越重启。
    _force_clear_xtest_mods()

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

    print("\n悬浮状态栏已显示 (左键可拖拽位置; 真停 daemon: systemctl --user stop ollama-voice-daemon)")

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
            name=_UINPUT_DEVICE_NAME,
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

    # 初始化快捷键 — XInput2 > evdev > XGrabKey 三级 fallback
    # XInput2: X server 客户端层 raw 事件, 能拦 SendEvent 注入按键 (KVM/串流)
    # evdev:   内核级, Wayland 兼容, 需 input 组权限
    # XGrabKey: 经典 X11 server-side grab, 仅物理 X11 按键
    recorder = VoiceRecorder()
    grabber = None
    for cls, note in (
        (XInputHotkeyGrabber, "XInput2 raw 事件 (KVM/SendEvent 友好)"),
        (EvdevHotkeyGrabber, "evdev 内核级"),
        (HotkeyGrabber, "XGrabKey 经典 X11"),
    ):
        try:
            grabber = cls(recorder)
            print(f"  快捷键: 启用 {cls.__name__} — {note}")
            break
        except Exception as e:
            print(f"  快捷键: {cls.__name__} 不可用 ({e}), 尝试下一个")
    if grabber is None:
        raise RuntimeError("所有快捷键抓取方式均不可用")
    _hotkey_grabber = grabber
    grabber.start()

    # 网页语音录入：后台长轮询线程，消费手机/其他电脑提交的转写文本并注入焦点窗口
    threading.Thread(target=_web_input_poll_loop, daemon=True).start()

    # 退出信号统一走干净退出，确保 finally 清理执行：
    #   SIGTERM = systemctl stop；SIGINT = 前台运行 Ctrl-C；SIGHUP = 终端关闭。
    # 此前仅处理 SIGTERM，前台 Ctrl-C/SIGHUP 退出时清理不保证执行 → 残留卡键根因之一。
    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(_sig, lambda s, f: GLib.idle_add(Gtk.main_quit))
    # 任意路径退出（含未捕获异常 / sys.exit / 主循环正常返回）兜底释放残留修饰键与
    # 指针 grab。SIGKILL 捕获不到，但内核会随 fd 关闭自动释放 uinput 按键。
    atexit.register(_panic_release_all)
    # 空闲兜底 watchdog：每 2s 断言无残留修饰键 / 指针 grab，偶发卡死 ~2s 内自愈，
    # 并向 systemd 喂狗。
    GLib.timeout_add(2000, _idle_safety_watchdog)

    # 通知 systemd 启动完成（Type=notify）。非 systemd 运行时为 no-op。
    _sd_notify("READY=1")

    # GTK 主循环
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        _panic_release_all()  # 先释放残留修饰键 + 指针 grab，再拆设备
        grabber.cleanup()
        if _uinput:
            try:
                _uinput.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
