#!/usr/bin/env python3
"""全局快捷键守护进程 —— 按住录音 / 截图翻译 / 常驻悬浮状态栏。

快捷键:
  Ctrl+`       按住录音，松开转写并自动粘贴到当前光标
  Alt+X        截图框选，OCR 翻译（结果浮窗显示在选区附近）

使用 XGrabKey (X11 服务器级别) 注册快捷键，最高优先级。
常驻悬浮窗显示实时状态。

需要系统工具: maim, xclip
启动: source .venv/bin/activate && python daemon.py
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

import httpx
import numpy as np
import sounddevice as sd
import soundfile as sf
from pynput.keyboard import Controller as KbController
from pynput.keyboard import Key
from Xlib import X, XK
from Xlib import display as xdisplay


# ---------------------------------------------------------------------------
# 配置（持久化到 .config.json）
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / ".config.json"

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:17945")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
VOICE_MODE = os.getenv("VOICE_MODE", "polish")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "zh")
SAMPLE_RATE = 16000

# 快捷键配置（可通过 .config.json 自定义）
HOTKEY_VOICE = "ctrl+grave"
HOTKEY_SCREENSHOT = "alt+x"
HOTKEY_REPEAT = "ctrl+shift+z"


def load_config() -> None:
    """从 .config.json 加载持久化配置，覆盖环境变量默认值。"""
    global \
        OLLAMA_MODEL, \
        VOICE_MODE, \
        WHISPER_LANGUAGE, \
        HOTKEY_VOICE, \
        HOTKEY_SCREENSHOT, \
        HOTKEY_REPEAT
    if not CONFIG_PATH.exists():
        return
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        OLLAMA_MODEL = cfg.get("ollama_model", OLLAMA_MODEL)
        VOICE_MODE = cfg.get("voice_mode", VOICE_MODE)
        WHISPER_LANGUAGE = cfg.get("whisper_language", WHISPER_LANGUAGE)
        HOTKEY_VOICE = cfg.get("hotkey_voice", HOTKEY_VOICE)
        HOTKEY_SCREENSHOT = cfg.get("hotkey_screenshot", HOTKEY_SCREENSHOT)
        HOTKEY_REPEAT = cfg.get("hotkey_repeat", HOTKEY_REPEAT)
    except Exception:
        pass


def save_config() -> None:
    """将当前配置写入 .config.json。"""
    cfg = {
        "ollama_model": OLLAMA_MODEL,
        "voice_mode": VOICE_MODE,
        "whisper_language": WHISPER_LANGUAGE,
        "hotkey_voice": HOTKEY_VOICE,
        "hotkey_screenshot": HOTKEY_SCREENSHOT,
        "hotkey_repeat": HOTKEY_REPEAT,
    }
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    except Exception:
        pass


# 全局状态栏引用（main() 中初始化）
_status_bar: StatusBar | None = None

# 全局翻译覆盖层引用
_translation_overlay: TranslationOverlay | None = None

# 全局快捷键抓取器引用（main() 中初始化）
_hotkey_grabber: HotkeyGrabber | None = None

# 全局 uinput 虚拟键盘 —— 内核级按键事件，不可被应用检测（main() 中初始化）
_uinput = None

# 上一次语音识别结果（用于 Ctrl+Shift+Z 重复输入）
_last_result = ""

# IBus 输入法总线（main() 中初始化）—— 用于直接提交文字到焦点窗口
_ibus_bus = None

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

_kb = KbController()


def set_status(text: str, style: str = "normal") -> None:
    """线程安全地更新悬浮状态栏。style: normal, recording, busy, error, success"""
    if _status_bar:
        GLib.idle_add(_status_bar.update_status, text, style)


def _request_regrab() -> bool:
    """在主线程中刷新快捷键抓取，防止合成按键导致 XGrabKey 失效。"""
    if _hotkey_grabber and not _hotkey_grabber.voice_active:
        try:
            _hotkey_grabber.ungrab()
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


def copy_to_clipboard(text: str) -> None:
    """跨平台剪贴板写入（X11 / Wayland 自适应）。"""
    session = os.environ.get("XDG_SESSION_TYPE", "x11")
    if session == "wayland" and shutil.which("wl-copy"):
        cmd = ["wl-copy"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard"]
    elif shutil.which("xsel"):
        cmd = ["xsel", "--clipboard", "--input"]
    else:
        set_status("剪贴板工具缺失，请安装 xclip", "error")
        return
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    proc.communicate(text.encode("utf-8"))


def paste_from_clipboard() -> None:
    """模拟 Ctrl+Shift+V 把剪贴板内容粘贴到当前聚焦的应用。"""
    time.sleep(0.1)
    _kb.press(Key.ctrl)
    _kb.press(Key.shift)
    _kb.tap("v")
    _kb.release(Key.shift)
    _kb.release(Key.ctrl)


def _try_ibus_commit(text: str) -> bool:
    """Tier 1: 通过 IBus 输入法框架直接提交文字到焦点窗口的输入上下文。

    这是真正的输入法级行为 —— 文字直接出现在光标处，
    无需经过剪贴板，对所有应用（终端/微信/QQ/IDE）一视同仁。
    """
    if _ibus_bus is None:
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
        ctx.commit_text(IBusLib.Text.new_from_string(text))
        return True
    except Exception:
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


def _type_text(text: str) -> None:
    """将文字输入到当前焦点窗口（三级策略）。

    Tier 1: IBus commit_text — 通过输入法框架直接提交（最可靠，无剪贴板副作用）
    Tier 2: 智能剪贴板粘贴 — 检测窗口类型，终端用 Ctrl+Shift+V，其他用 Ctrl+V
    Tier 3: 原始 Ctrl+V — pynput 回退（XTest 合成按键）
    """
    time.sleep(0.1)  # 等待焦点稳定

    # ---- Tier 1: IBus 输入法直接提交 ----
    if _try_ibus_commit(text):
        return

    # ---- Tier 2 & 3: 剪贴板粘贴 ----
    copy_to_clipboard(text)
    time.sleep(0.05)
    is_term = _is_terminal_focused()

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
            return
        except Exception:
            pass
    # Tier 3: pynput 回退
    _kb.press(Key.ctrl)
    if is_term:
        _kb.press(Key.shift)
    _kb.tap("v")
    if is_term:
        _kb.release(Key.shift)
    _kb.release(Key.ctrl)


def _get_clipboard_text() -> str:
    """读取当前剪贴板文本内容（用于保存/恢复）。"""
    session = os.environ.get("XDG_SESSION_TYPE", "x11")
    if session == "wayland" and shutil.which("wl-paste"):
        cmd = ["wl-paste", "--no-newline"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-o"]
    elif shutil.which("xsel"):
        cmd = ["xsel", "--clipboard", "--output"]
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
    border-radius: 22px;
    border: 1px solid rgba(100, 140, 255, 0.25);
    padding: 10px 24px;
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
.status-label {
    color: #c0c8e0;
    font-size: 14px;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
}
.status-recording .status-label { color: #fca5a5; }
.status-busy .status-label { color: #fcd34d; }
.status-error .status-label { color: #fca5a5; }
.status-success .status-label { color: #86efac; }
.status-hint {
    color: rgba(160, 168, 190, 0.5);
    font-size: 11px;
}

/* 翻译结果覆盖层 */
.trans-overlay {
    background-color: rgba(18, 18, 28, 0.93);
    border-radius: 8px;
    border: 1.5px solid rgba(60, 140, 255, 0.4);
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
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

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.ebox.add(hbox)

        # 状态圆点
        self.dot = Gtk.DrawingArea()
        self.dot.set_size_request(8, 8)
        self.dot.get_style_context().add_class("status-dot")
        hbox.pack_start(self.dot, False, False, 0)

        # 状态文字
        self.label = Gtk.Label()
        self.label.get_style_context().add_class("status-label")
        self.label.set_text(
            f"{_format_hotkey(HOTKEY_VOICE)}语音 | "
            f"{_format_hotkey(HOTKEY_SCREENSHOT)}截图 | "
            f"{_format_hotkey(HOTKEY_REPEAT)}重复"
        )
        hbox.pack_start(self.label, False, False, 0)

        # 分隔
        sep = Gtk.Label()
        sep.get_style_context().add_class("status-hint")
        sep.set_text("  \u2502 拖拽移动 \u2502 右键退出")
        hbox.pack_start(sep, False, False, 0)

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
        self.label.set_text(text)
        ctx = self.ebox.get_style_context()
        for s in ("status-recording", "status-busy", "status-error", "status-success"):
            ctx.remove_class(s)
        if style != "normal":
            ctx.add_class(f"status-{style}")

    def _on_press(self, widget, event):
        if event.button == 1:
            self._dragging = True
            self._drag_offset = (event.x_root, event.y_root)
            self._win_pos = self.get_position()
        elif event.button == 3:
            menu = Gtk.Menu()
            item = Gtk.MenuItem(label="退出")
            item.connect("activate", lambda _: Gtk.main_quit())
            menu.append(item)
            menu.show_all()
            menu.popup(None, None, None, None, event.button, event.time)

    def _on_release(self, widget, event):
        self._dragging = False

    def _on_motion(self, widget, event):
        if self._dragging:
            dx = event.x_root - self._drag_offset[0]
            dy = event.y_root - self._drag_offset[1]
            ox, oy = self._win_pos
            self.move(int(ox + dx), int(oy + dy))


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

        # EventBox 包裹（后续点击关闭）
        self._ebox = Gtk.EventBox()
        self._ebox.add(self._box)
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
            bottom.pack_start(copy_btn, False, False, 0)

        hint = Gtk.Label(label="点击外部 / Esc 关闭")
        hint.get_style_context().add_class("trans-hint")
        bottom.pack_start(hint, False, False, 0)

        self._box.pack_start(bottom, False, False, 0)

        # 点击空白关闭（Button 消费事件不会冒泡）
        self._ebox.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self._ebox.connect("button-press-event", lambda *_: self.destroy())

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

    def start(self) -> None:
        with self._lock:
            if self.recording:
                return
            self.recording = True
            self._frames = []

        set_status("录音中... 松开停止", "recording")
        _play_beep(400, 0.1)  # 低音提示: 开始录音

        def callback(indata, frames, time_info, status):
            if self.recording:
                self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()

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
            GLib.timeout_add(
                3000,
                lambda: (
                    set_status(
                        f"{_format_hotkey(HOTKEY_VOICE)}语音 | {_format_hotkey(HOTKEY_SCREENSHOT)}截图 | {_format_hotkey(HOTKEY_REPEAT)}重复"
                    ),
                    False,
                )[-1],
            )
            return

        audio = np.concatenate(self._frames, axis=0)
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
                data = {"mode": VOICE_MODE, "language": WHISPER_LANGUAGE}
                resp = httpx.post(
                    f"{API_BASE}/api/transcribe",
                    files=files,
                    data=data,
                    timeout=120,
                )
                if resp.status_code != 200:
                    try:
                        detail = resp.json().get("detail", resp.text[:200])
                    except Exception:
                        detail = resp.text[:200]
                    raise RuntimeError(detail)

            result = resp.json()
            final = result.get("final_text", "")
            if final:
                global _last_result
                _last_result = final
                old_clipboard = _get_clipboard_text()
                _type_text(final)
                time.sleep(0.3)
                # 仅在剪贴板仍是本次结果时恢复，避免覆盖用户新复制的内容
                current = _get_clipboard_text()
                if current == final:
                    copy_to_clipboard(old_clipboard)
                GLib.idle_add(_request_regrab)
                set_status("已输入", "success")
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
                        set_status(
                            f"{_format_hotkey(HOTKEY_VOICE)}语音 | {_format_hotkey(HOTKEY_SCREENSHOT)}截图 | {_format_hotkey(HOTKEY_REPEAT)}重复"
                        )
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
            set_status(
                f"{_format_hotkey(HOTKEY_VOICE)}语音 | {_format_hotkey(HOTKEY_SCREENSHOT)}截图 | {_format_hotkey(HOTKEY_REPEAT)}重复"
            )
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
                data={"target_lang": "zh"},
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
                set_status(
                    f"{_format_hotkey(HOTKEY_VOICE)}语音 | {_format_hotkey(HOTKEY_SCREENSHOT)}截图 | {_format_hotkey(HOTKEY_REPEAT)}重复"
                )
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
                self.ungrab()
                self.grab()
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
                    self.recorder.start()
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
            GLib.timeout_add(
                2000,
                lambda: (
                    set_status(
                        f"{_format_hotkey(HOTKEY_VOICE)}语音 | "
                        f"{_format_hotkey(HOTKEY_SCREENSHOT)}截图 | "
                        f"{_format_hotkey(HOTKEY_REPEAT)}重复"
                    ),
                    False,
                )[-1],
            )
            return
        set_status("重复输入中...", "busy")
        threading.Thread(target=self._do_repeat, daemon=True).start()

    def _do_repeat(self) -> None:
        """在后台线程执行重复输入（避免阻塞 GTK 主循环）。"""
        try:
            old_clipboard = _get_clipboard_text()
            _type_text(_last_result)
            time.sleep(0.3)
            current = _get_clipboard_text()
            if current == _last_result:
                copy_to_clipboard(old_clipboard)
            GLib.idle_add(_request_regrab)
            set_status("已重复输入", "success")
        except Exception as exc:
            set_status(f"重复输入失败: {str(exc)[:40]}", "error")
        GLib.timeout_add(
            3000,
            lambda: (
                set_status(
                    f"{_format_hotkey(HOTKEY_VOICE)}语音 | "
                    f"{_format_hotkey(HOTKEY_SCREENSHOT)}截图 | "
                    f"{_format_hotkey(HOTKEY_REPEAT)}重复"
                ),
                False,
            )[-1],
        )

    def _on_xlib_event(self, fd, condition) -> bool:
        try:
            self.dpy.sync()
            while self.dpy.pending_events() > 0:
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
# 启动自检
# ---------------------------------------------------------------------------


def startup_check() -> list[str]:
    issues = []

    session = os.environ.get("XDG_SESSION_TYPE", "x11")
    if session == "wayland":
        if not shutil.which("wl-copy"):
            issues.append("Wayland 环境缺少 wl-copy，请安装 wl-clipboard")
    else:
        if not shutil.which("xclip") and not shutil.which("xsel"):
            issues.append("缺少剪贴板工具: sudo apt install xclip")

    if not detect_screenshot_tool():
        if session == "wayland":
            issues.append("缺少截图工具: sudo apt install grim slurp")
        else:
            issues.append("缺少截图工具: sudo apt install maim")

    try:
        resp = httpx.get(f"{API_BASE}/api/status", timeout=3)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ollama_ok"):
            issues.append("Ollama 服务未连接，请先运行 ollama serve")
    except Exception:
        issues.append(f"API 服务器不可达: {API_BASE}")

    return issues


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> None:
    global _status_bar, _hotkey_grabber, _uinput, _ibus_bus

    # 加载持久化配置
    load_config()

    issues = startup_check()

    print("Ollama Voice Input 守护进程已启动")
    print(
        f"  语音输入: {_format_hotkey(HOTKEY_VOICE)}  按住说话，松开自动粘贴 (模式={VOICE_MODE})"
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

    # 初始化 uinput 虚拟键盘（内核级按键，不可被应用检测）
    try:
        from evdev import UInput, ecodes

        _uinput = UInput(
            {ecodes.EV_KEY: [ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_V]},
            name="ollama-voice-input",
        )
        print("  uinput: 已初始化（内核级按键模式）")
    except Exception as e:
        print(f"  uinput: 不可用，使用 pynput 回退 ({e})")
        _uinput = None

    # 初始化 IBus 输入法总线（用于 Tier 1 直接提交文字）
    try:
        gi.require_version("IBus", "1.0")
        from gi.repository import IBus as IBusLib

        _ibus_bus_tmp = IBusLib.Bus()
        if _ibus_bus_tmp.is_connected():
            _ibus_bus = _ibus_bus_tmp
            print("  IBus: 已连接（输入法直接提交模式）")
        else:
            print("  IBus: 守护进程未运行，使用剪贴板模式")
    except Exception as e:
        print(f"  IBus: 不可用 ({e})，使用剪贴板模式")

    # 初始化快捷键
    recorder = VoiceRecorder()
    grabber = HotkeyGrabber(recorder)
    _hotkey_grabber = grabber
    grabber.start()

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
