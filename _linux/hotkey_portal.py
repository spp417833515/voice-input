"""xdg-desktop-portal GlobalShortcuts 后端（Wayland 主路径）。"""
from __future__ import annotations

import os
import time
import traceback

from gi.repository import GLib

from _hotkey import to_portal_trigger
from _linux.hotkey_actions import HotkeyActionMixin, HotkeyContext
from _linux.portal_dbus import (
    GLOBAL_SHORTCUTS_IFACE,
    PORTAL_NAME,
    PORTAL_PATH,
    dbus_module,
    portal_available,
    portal_interface_version,
    register_portal_host_app,
)


class PortalGlobalShortcutsGrabber(HotkeyActionMixin):
    """xdg-desktop-portal GlobalShortcuts 后端（Wayland 主路径）。

    compositor 独占按键，并通过 Activated/Deactivated 提供原生按下/松开；
    不需要补发修饰键释放，也不再和 GNOME/应用抢同一组键。
    """

    _release_modifiers_on_stop = False

    def __init__(self, ctx: HotkeyContext, on_bound=None, on_failed=None) -> None:
        self._init_hotkey_actions(ctx)
        self._on_bound_cb = on_bound
        self._on_failed_cb = on_failed
        self._dbus = None
        self._bus = None
        self._iface = None
        self._session_handle: str | None = None
        self._signal_matches: list = []
        self._request_matches: list = []
        self._closed = False
        self._failed = False
        self._bound = False
        self.actual_triggers: dict[str, str] = {}

        version = portal_interface_version(GLOBAL_SHORTCUTS_IFACE)
        if version is None:
            raise RuntimeError("xdg-desktop-portal GlobalShortcuts 不可用")
        if version < 1:
            raise RuntimeError(f"GlobalShortcuts 版本过低: {version}")

        try:
            self._dbus = dbus_module()
            self._bus = self._dbus.SessionBus()
            obj = self._bus.get_object(PORTAL_NAME, PORTAL_PATH)
            self._iface = self._dbus.Interface(obj, GLOBAL_SHORTCUTS_IFACE)
        except Exception as exc:
            raise RuntimeError(f"连接 portal GlobalShortcuts 失败: {exc}") from exc

        self._connect_signals()
        register_portal_host_app(self._bus)
        self._owner_watch = None
        try:
            self._owner_watch = self._bus.watch_name_owner(
                PORTAL_NAME, self._on_portal_owner_changed
            )
        except Exception:
            pass

    def _on_portal_owner_changed(self, owner) -> None:
        if not owner:
            self._fail("xdg-desktop-portal 服务已退出")

    @classmethod
    def available(cls) -> bool:
        return portal_available(GLOBAL_SHORTCUTS_IFACE)

    def _connect_signals(self) -> None:
        for name, cb in (
            ("Activated", self._on_activated),
            ("Deactivated", self._on_deactivated),
            ("ShortcutsChanged", self._on_shortcuts_changed),
        ):
            try:
                self._signal_matches.append(self._iface.connect_to_signal(name, cb))
            except Exception as exc:
                raise RuntimeError(f"订阅 portal 信号 {name} 失败: {exc}") from exc

    @staticmethod
    def _new_token(prefix: str) -> str:
        return f"{prefix}_{os.getpid()}_{int(time.time() * 1000) % 100000000}"

    def _watch_response(self, handle, callback) -> None:
        req = self._bus.get_object(PORTAL_NAME, handle)
        match = self._dbus.Interface(
            req, "org.freedesktop.portal.Request"
        ).connect_to_signal("Response", callback)
        self._request_matches.append((req, match))

    def start(self) -> None:
        if self._closed:
            return
        self._create_session()

    def _create_session(self) -> None:
        token = self._new_token("ovi_session")
        options = self._dbus.Dictionary(
            {
                "handle_token": self._dbus.String(token),
                "session_handle_token": self._dbus.String(token),
            },
            signature="sv",
        )
        try:
            handle = self._iface.CreateSession(options)
            self._watch_response(handle, self._on_session_response)
        except Exception as exc:
            self._fail(f"CreateSession 调用失败: {exc}")

    def _on_session_response(self, response, results) -> None:
        if self._closed:
            return
        if int(response) != 0:
            self._fail(f"CreateSession 被拒绝 (response={int(response)})")
            return
        session = results.get("session_handle") if results else None
        if not session:
            self._fail("CreateSession 未返回 session_handle")
            return
        self._session_handle = str(session)
        self._bind_shortcuts()

    def _shortcut_specs(self) -> tuple[tuple[str, str, str], ...]:
        return (
            ("voice", "语音输入", self.ctx.bindings.voice),
            ("screenshot", "截图翻译", self.ctx.bindings.screenshot),
            ("repeat", "重复输入", self.ctx.bindings.repeat),
        )

    def _bind_shortcuts(self) -> None:
        shortcuts = []
        for sid, desc, hotkey in self._shortcut_specs():
            props = self._dbus.Dictionary(
                {
                    "description": self._dbus.String(desc),
                    "preferred_trigger": self._dbus.String(to_portal_trigger(hotkey)),
                },
                signature="sv",
            )
            shortcuts.append((self._dbus.String(sid), props))
        token = self._new_token("ovi_bind")
        options = self._dbus.Dictionary(
            {"handle_token": self._dbus.String(token)}, signature="sv"
        )
        try:
            handle = self._iface.BindShortcuts(
                self._dbus.ObjectPath(self._session_handle),
                self._dbus.Array(shortcuts, signature="(sa{sv})"),
                "",
                options,
            )
            self._watch_response(handle, self._on_bind_response)
        except Exception as exc:
            self._fail(f"BindShortcuts 调用失败: {exc}")

    def _parse_shortcuts(self, raw) -> dict[str, str]:
        out: dict[str, str] = {}
        if not raw:
            return out
        try:
            for sid, props in raw:
                trigger = (
                    props.get("trigger")
                    or props.get("preferred_trigger")
                    or props.get("trigger_description")
                    or ""
                )
                out[str(sid)] = str(trigger)
        except Exception:
            pass
        return out

    def _on_bind_response(self, response, results) -> None:
        if self._closed:
            return
        if int(response) != 0:
            self._fail(f"BindShortcuts 被拒绝 (response={int(response)})")
            return
        self._bound = True
        self.actual_triggers = self._parse_shortcuts(
            results.get("shortcuts") if results else None
        )
        print("  快捷键: portal GlobalShortcuts 已绑定", flush=True)
        for sid, trigger in self.actual_triggers.items():
            print(f"    {sid}: {trigger}", flush=True)
        if self._on_bound_cb:
            try:
                self._on_bound_cb()
            except Exception:
                traceback.print_exc()

    def _on_shortcuts_changed(self, session_handle, shortcuts) -> None:
        if self._session_handle and str(session_handle) != self._session_handle:
            return
        self.actual_triggers = self._parse_shortcuts(shortcuts)
        print(f"  快捷键: portal 键位已更新 {self.actual_triggers}", flush=True)

    def _on_activated(self, session_handle, shortcut_id, timestamp, options) -> None:
        if self._session_handle and str(session_handle) != self._session_handle:
            return
        sid = str(shortcut_id)
        GLib.idle_add(self._idle_action, sid)

    def _idle_action(self, sid: str) -> bool:
        if self._closed:
            return False
        if sid == "voice":
            self._on_voice_press()
        elif sid == "screenshot":
            self._on_screenshot()
        elif sid == "repeat":
            self._on_repeat()
        return False

    def _on_deactivated(self, session_handle, shortcut_id, timestamp, options) -> None:
        if self._session_handle and str(session_handle) != self._session_handle:
            return
        if str(shortcut_id) == "voice":
            GLib.idle_add(self._idle_stop)

    def _idle_stop(self) -> bool:
        if not self._closed:
            self._do_release()
        return False

    def _fail(self, message: str) -> None:
        if self._failed or self._closed:
            return
        self._failed = True
        print(f"  快捷键: portal GlobalShortcuts 失败: {message}", flush=True)
        if self._on_failed_cb:
            try:
                self._on_failed_cb()
            except Exception:
                traceback.print_exc()

    def grab(self) -> None:
        pass

    def ungrab(self) -> None:
        pass

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_pending_release()
        for _req, match in self._request_matches:
            try:
                match.remove()
            except Exception:
                pass
        self._request_matches.clear()
        for match in self._signal_matches:
            try:
                match.remove()
            except Exception:
                pass
        self._signal_matches.clear()
        if self._owner_watch is not None:
            try:
                self._bus.unwatch_name_owner(self._owner_watch)
            except Exception:
                pass
            self._owner_watch = None
        if self._session_handle:
            try:
                sess = self._bus.get_object(PORTAL_NAME, self._session_handle)
                self._dbus.Interface(
                    sess, "org.freedesktop.portal.Session"
                ).Close()
            except Exception:
                pass
        self._session_handle = None
