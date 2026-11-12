"""扫描 GNOME 桌面级快捷键冲突（只读 gsettings）。不修改任何系统设置。

读取来源：5 个 GNOME 键位 schema、media-keys 自定义快捷键、GNOME 全局快捷键授权表
`org.gnome.settings-daemon.global-shortcuts`（其它应用经 portal 登记的组合键；排除本应用 app id）。
只报告实际读到并经 `normalize_accel` 解析的登记；任一来源读取/解析失败记入 `unread`，
因此「conflicts 为空」只表示「已读来源中未发现」，不等于无冲突。第三方 evdev 监听与应用内快捷键无法探测。
"""
from __future__ import annotations

import ast
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from _hotkey import HotkeyBindings, normalize_accel
from _linux.portal_dbus import OWN_APP_IDS

Runner = Callable[..., subprocess.CompletedProcess]

KEYBINDING_SCHEMAS = (
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.settings-daemon.plugins.media-keys",
    "org.gnome.mutter.keybindings",
    "org.gnome.mutter.wayland.keybindings",
)
MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GLOBAL_SHORTCUTS_SCHEMA = "org.gnome.settings-daemon.global-shortcuts"
GLOBAL_SHORTCUTS_PATH = "/org/gnome/settings-daemon/global-shortcuts"


@dataclass(frozen=True)
class ConflictScan:
    """conflicts：已读到的冲突说明；unread：未能读取/解析的来源及原因（非空时不能宣称无冲突）。"""

    conflicts: tuple[str, ...]
    unread: tuple[str, ...]


class _Unread(Exception):
    pass


def _gsettings(run: Runner, args: list[str]) -> str:
    cmd = ["gsettings", *args]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _Unread(f"{type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise _Unread(f"退出码 {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:200]}")
    return proc.stdout or ""


def _strip_type(raw: str) -> str:
    """去掉 GVariant 文本的类型注记（如 `@as []`、`@a(sa{sv}) []`）。"""
    raw = raw.strip()
    if raw.startswith("@"):
        _, _, raw = raw.partition(" ")
    return raw.strip()


def _drop_variant_brackets(raw: str) -> str:
    """GVariant 的 variant 写作 <value>；去掉引号外的尖括号，引号内（如 '<Control>z'）原样保留。"""
    out: list[str] = []
    quote = ""
    escaped = False
    for ch in raw:
        if quote:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch not in "<>":
            out.append(ch)
    return "".join(out)


def _parse_string_list(raw: str) -> list[str] | None:
    """解析 `['<Super>a']` / `'<Alt>z'` / `@as []`；不是字符串或字符串列表类型返回 None（不是快捷键键值）。
    看起来是字符串/列表但解析失败抛 _Unread。"""
    text = _strip_type(raw)
    if not text or text[0] not in "['\"":
        return None
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError) as exc:
        raise _Unread(f"无法解析 {text[:80]!r}") from exc
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def parse_global_shortcuts(raw: str) -> list[tuple[str, list[str]]]:
    """解析一个应用的 global-shortcuts `shortcuts` 值：[(shortcut_id, [accel...])]。
    只取实际出现 'shortcuts' 的条目；格式不符抛 _Unread。"""
    text = _strip_type(raw)
    try:
        value = ast.literal_eval(_drop_variant_brackets(text))
    except (ValueError, SyntaxError) as exc:
        raise _Unread(f"无法解析 {text[:80]!r}") from exc
    if not isinstance(value, list):
        raise _Unread(f"不是列表：{text[:80]!r}")
    entries: list[tuple[str, list[str]]] = []
    for item in value:
        if not (isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) and isinstance(item[1], dict)):
            raise _Unread(f"条目格式不符：{str(item)[:80]!r}")
        accels = item[1].get("shortcuts")
        if accels is None:
            continue
        if not (isinstance(accels, list) and all(isinstance(a, str) for a in accels)):
            raise _Unread(f"{item[0]} 的 shortcuts 不是字符串列表")
        entries.append((item[0], accels))
    return entries


def scan_hotkey_conflicts(
    bindings: HotkeyBindings,
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> ConflictScan:
    """只读扫描；返回已读到的冲突与未能读取的来源。"""
    if not which("gsettings"):
        return ConflictScan(conflicts=(), unread=("gsettings 不可用，未能读取任何 GNOME 快捷键登记",))
    wanted = {
        "voice": normalize_accel(bindings.voice),
        "screenshot": normalize_accel(bindings.screenshot),
        "repeat": normalize_accel(bindings.repeat),
    }
    conflicts: list[str] = []
    unread: list[str] = []

    def check(where: str, accel: str) -> None:
        if not accel:
            return
        norm = normalize_accel(accel)
        for name, hk in wanted.items():
            msg = f"{name} {hk} 与 {where} 的 {accel} 冲突"
            if hk and norm == hk and msg not in conflicts:
                conflicts.append(msg)

    for schema in KEYBINDING_SCHEMAS:
        try:
            out = _gsettings(run, ["list-recursively", schema])
            for line in out.splitlines():
                parts = line.split(" ", 2)
                if len(parts) < 3:
                    continue
                accels = _parse_string_list(parts[2])
                for accel in accels or []:
                    check(f"{schema} {parts[1]}", accel)
        except _Unread as exc:
            unread.append(f"{schema}：{exc}")

    try:
        paths = _parse_string_list(_gsettings(run, ["get", MEDIA_KEYS_SCHEMA, "custom-keybindings"]))
        if paths is None:
            raise _Unread("custom-keybindings 不是字符串列表")
    except _Unread as exc:
        unread.append(f"自定义快捷键列表：{exc}")
        paths = []
    for path in paths:
        try:
            accels = _parse_string_list(
                _gsettings(run, ["get", f"{MEDIA_KEYS_SCHEMA}.custom-keybinding:{path}", "binding"])
            )
            if accels is None:
                raise _Unread("binding 不是字符串")
            for accel in accels:
                check(f"自定义快捷键 {path}", accel)
        except _Unread as exc:
            unread.append(f"自定义快捷键 {path}：{exc}")

    try:
        apps = _parse_string_list(_gsettings(run, ["get", GLOBAL_SHORTCUTS_SCHEMA, "applications"]))
        if apps is None:
            raise _Unread("applications 不是字符串列表")
    except _Unread as exc:
        unread.append(f"GNOME 全局快捷键授权表：{exc}")
        apps = []
    for app in apps:
        if app in OWN_APP_IDS:
            continue
        try:
            raw = _gsettings(
                run,
                ["get", f"{GLOBAL_SHORTCUTS_SCHEMA}.application:{GLOBAL_SHORTCUTS_PATH}/{app}/", "shortcuts"],
            )
            for shortcut_id, accels in parse_global_shortcuts(raw):
                for accel in accels:
                    check(f"应用 {app} 的全局快捷键 {shortcut_id}", accel)
        except _Unread as exc:
            unread.append(f"应用 {app} 的全局快捷键：{exc}")

    return ConflictScan(conflicts=tuple(conflicts), unread=tuple(unread))


def describe_scan(scan: ConflictScan) -> list[str]:
    """日志/提示用的逐行说明：先冲突，再「未能读取」。"""
    return [*scan.conflicts, *(f"未能读取快捷键登记（不能据此判定无冲突）：{item}" for item in scan.unread)]
