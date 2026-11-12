"""xdg-desktop-portal 的 D-Bus 公共部分：主循环、接口版本、host app 注册。

Wayland 下由 compositor 独占按键 / 提供整屏截图。dbus-python 必须接到 GLib 主循环，
portal 信号才会在 GTK 主循环里派发。
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
GLOBAL_SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"
SCREENSHOT_IFACE = "org.freedesktop.portal.Screenshot"

# 本应用向 portal host Registry 登记的 app id（GNOME 全局快捷键授权表里以它为键）。唯一来源。
DAEMON_APP_ID = "ollama-voice-input-daemon"  # 装了 ~/.local/share/applications/<id>.desktop 时
FALLBACK_APP_ID = "ollama-voice-input"  # 未装时退回项目内 .desktop
OWN_APP_IDS = (DAEMON_APP_ID, FALLBACK_APP_ID)

_dbus_loop_ready = False


def ensure_dbus_glib_loop() -> bool:
    """把 dbus-python 的默认主循环接到 GLib，保证 portal 信号在 GTK 主循环里派发。"""
    global _dbus_loop_ready
    if _dbus_loop_ready:
        return True
    try:
        import dbus.mainloop.glib

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        _dbus_loop_ready = True
        return True
    except Exception:
        return False


def dbus_module():
    if not ensure_dbus_glib_loop():
        raise RuntimeError("dbus-python / GLib 主循环不可用")
    import dbus

    return dbus


def portal_interface_version(interface: str) -> int | None:
    """返回 portal 接口版本；不存在/不可达返回 None。"""
    try:
        dbus = dbus_module()
        bus = dbus.SessionBus()
        obj = bus.get_object(PORTAL_NAME, PORTAL_PATH)
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        return int(props.Get(interface, "version"))
    except Exception:
        return None


def portal_available(interface: str) -> bool:
    return portal_interface_version(interface) is not None



HOST_REGISTRY_IFACE = "org.freedesktop.host.portal.Registry"
_PORTAL_HOST_REGISTERED = False


def register_portal_host_app(bus=None) -> bool:
    """把本进程注册成 host app，给 xdg-desktop-portal 一个明确的 app_id。

    systemd 服务没有桌面上下文，门户会报 "An app id is required"；
    org.freedesktop.host.portal.Registry 是官方给非沙箱宿主应用的注册入口。
    """
    global _PORTAL_HOST_REGISTERED
    if _PORTAL_HOST_REGISTERED:
        return True
    try:
        dbus = dbus_module()
        if bus is None:
            bus = dbus.SessionBus()
        obj = bus.get_object(PORTAL_NAME, PORTAL_PATH)
        iface = dbus.Interface(obj, HOST_REGISTRY_IFACE)
        desktop = Path.home() / f".local/share/applications/{DAEMON_APP_ID}.desktop"
        app_id = DAEMON_APP_ID
        if not desktop.exists():
            desktop = PROJECT_ROOT / f"{FALLBACK_APP_ID}.desktop"
            app_id = FALLBACK_APP_ID
        options = dbus.Dictionary(
            {"desktop_entry": dbus.String(str(desktop))}, signature="sv"
        )
        iface.Register(app_id, options)
        _PORTAL_HOST_REGISTERED = True
        print(f"  快捷键: portal host app 已注册 ({app_id})", flush=True)
        return True
    except Exception as exc:
        print(f"  快捷键: portal host app 注册失败 ({exc})", flush=True)
        return False
