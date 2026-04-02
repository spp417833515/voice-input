#!/usr/bin/env python3
"""截图框选 → OCR翻译 → 浮窗显示 → 右键/Esc 退出

体验流程：
  1. maim -s 冻结屏幕，用户拖拽框选
  2. 选区图片发送到 OCR 翻译 API
  3. 翻译结果显示在鼠标附近的浮窗中
  4. 右键点击或按 Esc 退出

用法: python3 screenshot_ui.py
环境变量: API_BASE, TARGET_LANG
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

import httpx

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:17945")
TARGET_LANG = os.environ.get("TARGET_LANG", "zh")

# CSS 样式
CSS = b"""
.result-window {
    background-color: rgba(18, 18, 28, 0.95);
    border-radius: 12px;
    border: 1.5px solid rgba(60, 140, 255, 0.4);
}
.result-text {
    color: #d8dff2;
    font-size: 15px;
    padding: 16px 20px;
}
.result-error {
    color: #ff8888;
    font-size: 14px;
    padding: 16px 20px;
}
.loading-box {
    padding: 18px 28px;
}
.loading-label {
    color: #aab0c0;
    font-size: 13px;
    margin-left: 10px;
}
.hint-label {
    color: rgba(180, 180, 195, 0.7);
    font-size: 11px;
    padding: 4px 20px 10px 20px;
}
"""


# ---------------------------------------------------------------------------
# 剪贴板
# ---------------------------------------------------------------------------

def copy_to_clipboard(text: str) -> None:
    session = os.environ.get("XDG_SESSION_TYPE", "x11")
    if session == "wayland" and shutil.which("wl-copy"):
        cmd = ["wl-copy"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard"]
    elif shutil.which("xsel"):
        cmd = ["xsel", "--clipboard", "--input"]
    else:
        return
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    proc.communicate(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# maim 选区截图
# ---------------------------------------------------------------------------

def capture_selection(output_path: str) -> bool:
    """使用 maim -s 冻结屏幕并让用户框选，保存选区到文件。"""
    if not shutil.which("maim"):
        print("未找到 maim，请安装: sudo apt install maim", file=sys.stderr)
        return False
    result = subprocess.run(
        ["maim", "-s", output_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# 结果浮窗
# ---------------------------------------------------------------------------

class ResultPopup(Gtk.Window):
    """轻量浮窗，显示翻译结果或加载状态。"""

    def __init__(self, image_path: str) -> None:
        super().__init__(type=Gtk.WindowType.POPUP)

        self.image_path = image_path

        # 应用 CSS
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
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
        self.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)

        # RGBA 透明
        screen = Gdk.Screen.get_default()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)

        # 主容器
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.box.get_style_context().add_class("result-window")
        self.add(self.box)

        # 加载状态
        self.spinner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.spinner_box.get_style_context().add_class("loading-box")
        spinner = Gtk.Spinner()
        spinner.start()
        spinner.set_size_request(20, 20)
        self.spinner_box.pack_start(spinner, False, False, 0)
        loading_label = Gtk.Label(label="正在识别翻译中…")
        loading_label.get_style_context().add_class("loading-label")
        self.spinner_box.pack_start(loading_label, False, False, 0)
        self.box.pack_start(self.spinner_box, False, False, 0)

        # 结果文本（先隐藏）
        self.result_label = Gtk.Label()
        self.result_label.set_line_wrap(True)
        self.result_label.set_max_width_chars(60)
        self.result_label.set_xalign(0)
        self.result_label.set_selectable(True)
        self.result_label.get_style_context().add_class("result-text")
        self.box.pack_start(self.result_label, False, False, 0)
        self.result_label.hide()

        # 提示文本
        self.hint_label = Gtk.Label(label="右键或 Esc 退出")
        self.hint_label.get_style_context().add_class("hint-label")
        self.hint_label.set_xalign(0)
        self.box.pack_start(self.hint_label, False, False, 0)

        # 事件
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
        )
        self.connect("button-press-event", self.on_button_press)
        self.connect("key-press-event", self.on_key_press)

        # 设置可聚焦以接收键盘事件
        self.set_can_focus(True)

        # 定位到鼠标附近
        self._position_near_mouse()
        self.show_all()
        self.result_label.hide()

        # grab 键盘以接收 Esc
        GLib.idle_add(self._grab_keyboard)

        # 后台翻译
        threading.Thread(target=self._translate, daemon=True).start()

    def _grab_keyboard(self) -> bool:
        win = self.get_window()
        if win:
            seat = Gdk.Display.get_default().get_default_seat()
            seat.grab(
                win,
                Gdk.SeatCapabilities.KEYBOARD,
                False, None, None, None,
            )
        return False

    def _position_near_mouse(self) -> None:
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        device = seat.get_pointer()
        screen, mx, my = device.get_position()
        scr_w = screen.get_width()
        scr_h = screen.get_height()

        # 先设个初始大小
        self.set_default_size(280, 80)
        # 弹窗在鼠标下方偏右
        px = mx + 15
        py = my + 15
        # 边界检查
        if px + 400 > scr_w:
            px = mx - 400
        if py + 200 > scr_h:
            py = my - 200
        px = max(10, px)
        py = max(10, py)
        self.move(px, py)
        # 记住位置以便结果刷新后重新定位
        self._pos = (px, py)

    def on_button_press(self, widget, event):
        if event.button == 3:  # 右键
            self._quit()
        return True

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self._quit()
        return True

    def _quit(self):
        seat = Gdk.Display.get_default().get_default_seat()
        seat.ungrab()
        Gtk.main_quit()

    # ---- 翻译 ----

    def _translate(self) -> None:
        try:
            with open(self.image_path, "rb") as f:
                img_data = f.read()

            resp = httpx.post(
                f"{API_BASE}/api/ocr_translate",
                files={"image": ("crop.png", img_data, "image/png")},
                data={"target_lang": TARGET_LANG},
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
                copy_to_clipboard(text)
                GLib.idle_add(self._show_result, text)
            else:
                GLib.idle_add(self._show_error, "没有识别到文字")
        except Exception as exc:
            GLib.idle_add(self._show_error, str(exc)[:300])

    def _show_result(self, text: str) -> None:
        self.spinner_box.hide()
        self.result_label.set_text(text)
        self.result_label.get_style_context().remove_class("result-error")
        self.result_label.get_style_context().add_class("result-text")
        self.result_label.show()
        self.hint_label.set_text("右键或 Esc 退出  |  已复制到剪贴板")
        self._reposition()

    def _show_error(self, msg: str) -> None:
        self.spinner_box.hide()
        self.result_label.set_text(f"错误: {msg}")
        self.result_label.get_style_context().remove_class("result-text")
        self.result_label.get_style_context().add_class("result-error")
        self.result_label.show()
        self._reposition()

    def _reposition(self) -> None:
        """内容变化后可能需要调整窗口大小和位置。"""
        self.resize(1, 1)  # 让 GTK 重新计算最小尺寸


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    # 1. maim 选区截图
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name

    if not capture_selection(tmp_path):
        # 用户按 Esc 取消，或 maim 失败
        Path(tmp_path).unlink(missing_ok=True)
        sys.exit(0)

    try:
        # 2. 显示翻译浮窗
        _popup = ResultPopup(tmp_path)
        Gtk.main()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
