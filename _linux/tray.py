"""系统托盘（AyatanaAppIndicator3 → AppIndicator3 → 无托盘回退）。"""
from __future__ import annotations

import subprocess
import webbrowser
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, Gtk  # noqa: E402

_HAS_INDICATOR = False
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # noqa: E402

    _HAS_INDICATOR = True
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3  # noqa: E402

        _HAS_INDICATOR = True
    except (ValueError, ImportError):
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ICON = str(PROJECT_ROOT / "resources" / "ollama-voice-input.svg")
VERSION = "3.0.0"


def show_about(_item=None) -> None:
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
    try:
        logo = GdkPixbuf.Pixbuf.new_from_file_at_scale(APP_ICON, 128, 128, True)
        dialog.set_logo(logo)
    except Exception:
        dialog.set_logo_icon_name("audio-input-microphone")
    dialog.run()
    dialog.destroy()


def quit_daemon(_item=None) -> None:
    # systemctl stop 让 systemd 感知是主动停止, 不触发 Restart=always; 否则 3 秒后自愈
    subprocess.Popen(["systemctl", "--user", "stop", "ollama-voice-daemon"])


def create_tray_icon(*, settings_url: str, toggle_status: Callable[[bool], None]) -> object | None:
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
    toggle = Gtk.CheckMenuItem(label="显示悬浮状态栏")
    toggle.set_active(True)
    toggle.connect("toggled", lambda item: toggle_status(item.get_active()))
    menu.append(toggle)
    menu.append(Gtk.SeparatorMenuItem())

    settings_item = Gtk.MenuItem(label="设置")
    settings_item.connect("activate", lambda _item: webbrowser.open(settings_url))
    menu.append(settings_item)

    about_item = Gtk.MenuItem(label="关于")
    about_item.connect("activate", show_about)
    menu.append(about_item)
    menu.append(Gtk.SeparatorMenuItem())

    # 退出: 调 systemctl --user stop, systemd 收到主动停止信号不会触发 Restart=always
    quit_item = Gtk.MenuItem(label="退出")
    quit_item.connect("activate", quit_daemon)
    menu.append(quit_item)

    menu.show_all()
    indicator.set_menu(menu)
    print("  托盘: 系统托盘图标已创建 ✓")
    return indicator
