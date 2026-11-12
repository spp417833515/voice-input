"""冲突扫描（只读 gsettings）的假 runner 用例：GNOME 全局快捷键授权表、排除本应用、读取失败进 unread。"""
from __future__ import annotations

import subprocess

from _hotkey import HotkeyBindings
from _linux.conflicts import parse_global_shortcuts, scan_hotkey_conflicts
from _linux.portal_dbus import DAEMON_APP_ID, FALLBACK_APP_ID

GS = "org.gnome.settings-daemon.global-shortcuts"
APP_PATH = f"{GS}.application:/org/gnome/settings-daemon/global-shortcuts"
# 本机 06:3x EST 只读 gsettings get 的真实格式（GNOME 50）
MANGGO = (
    "[('dictation', {'shortcuts': <['<Alt>z']>, 'description': <'语音输入'>}), "
    "('screenshot-recognition', {'shortcuts': <['<Alt>x']>, 'description': <'截图识别'>})]"
)
OWN = "[('voice', {'shortcuts': <['<Control><Alt>z']>, 'description': <'语音输入'>})]"
NO_SHORTCUTS = "[('main_panel_visibility', {'description': <'显示/隐藏窗口'>})]"
BINDINGS = HotkeyBindings(voice="ctrl+alt+z", screenshot="alt+x", repeat="ctrl+alt+r")


def fake_run(table: dict[tuple[str, ...], tuple[int, str]], calls: list | None = None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        if tuple(cmd[1:2]) == ("list-recursively",):
            rc, out = table.get(tuple(cmd[1:]), (0, ""))
        else:
            rc, out = table.get(tuple(cmd[1:]), (0, "@as []"))
        return subprocess.CompletedProcess(cmd, rc, out if rc == 0 else "", "" if rc == 0 else out)

    return run


def base_table(apps: str) -> dict:
    return {
        ("get", GS, "applications"): (0, apps),
        ("get", f"{APP_PATH}/com.pylogmon.Manggo/", "shortcuts"): (0, MANGGO),
        ("get", f"{APP_PATH}/{DAEMON_APP_ID}/", "shortcuts"): (0, OWN),
        ("get", f"{APP_PATH}/{FALLBACK_APP_ID}/", "shortcuts"): (0, OWN),
        ("get", f"{APP_PATH}/wechat/", "shortcuts"): (0, NO_SHORTCUTS),
    }


def test_global_shortcuts_conflict_reported_and_own_app_excluded():
    calls: list = []
    apps = f"['{DAEMON_APP_ID}', '{FALLBACK_APP_ID}', 'wechat', 'com.pylogmon.Manggo']"
    scan = scan_hotkey_conflicts(BINDINGS, run=fake_run(base_table(apps), calls), which=lambda n: "/usr/bin/gsettings")
    assert scan.unread == ()
    assert scan.conflicts == ("screenshot alt+x 与 应用 com.pylogmon.Manggo 的全局快捷键 screenshot-recognition 的 <Alt>x 冲突",)
    read_apps = [c[2] for c in calls if c[1] == "get" and c[2].startswith(APP_PATH)]
    assert all(DAEMON_APP_ID not in a and not a.endswith(f"/{FALLBACK_APP_ID}/") for a in read_apps), read_apps


def test_entries_without_shortcuts_are_not_guessed():
    assert parse_global_shortcuts(NO_SHORTCUTS) == []
    assert parse_global_shortcuts("@a(sa{sv}) []") == []
    assert parse_global_shortcuts(MANGGO) == [("dictation", ["<Alt>z"]), ("screenshot-recognition", ["<Alt>x"])]


def test_read_failures_go_to_unread_not_no_conflict():
    table = base_table("['com.pylogmon.Manggo', 'broken']")
    table[("list-recursively", "org.gnome.mutter.wayland.keybindings")] = (1, "No such schema “org.gnome.mutter.wayland.keybindings”")
    table[("get", f"{APP_PATH}/broken/", "shortcuts")] = (0, "[('x', {'shortcuts': <oops>})]")
    scan = scan_hotkey_conflicts(BINDINGS, run=fake_run(table), which=lambda n: "/usr/bin/gsettings")
    assert len(scan.conflicts) == 1
    assert any("org.gnome.mutter.wayland.keybindings" in u and "No such schema" in u for u in scan.unread), scan.unread
    assert any("broken" in u for u in scan.unread), scan.unread


def test_missing_global_shortcuts_schema_is_unread():
    table = {("get", GS, "applications"): (1, "No such schema “org.gnome.settings-daemon.global-shortcuts”")}
    scan = scan_hotkey_conflicts(BINDINGS, run=fake_run(table), which=lambda n: "/usr/bin/gsettings")
    assert scan.conflicts == ()
    assert any("全局快捷键授权表" in u for u in scan.unread), scan.unread


def test_runner_exception_is_unread():
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 2)

    scan = scan_hotkey_conflicts(BINDINGS, run=run, which=lambda n: "/usr/bin/gsettings")
    assert scan.conflicts == ()
    assert len(scan.unread) == 5 + 1 + 1  # 5 个键位 schema + 自定义列表 + 授权表
    assert all("TimeoutExpired" in u for u in scan.unread)
