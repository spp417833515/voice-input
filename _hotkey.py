"""快捷键字符串的纯逻辑：规范化、显示、Portal 触发名、GNOME accelerator、evdev 键码。

不抓取真实键鼠，不导入 Xlib/GTK。默认键位 Ctrl+Alt+Z/X/R 的唯一来源是 `_contracts.AppConfig`。
抓取后端在 `_linux/hotkey_*.py`，都从这里取解析结果，不各写一份。
"""
from __future__ import annotations

from dataclasses import dataclass

MOD_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "primary": "ctrl",
    "option": "alt",
    "mod1": "alt",
    "mod4": "super",
    "win": "super",
    "cmd": "super",
}


@dataclass(frozen=True)
class HotkeyBindings:
    """一次启动内三条快捷键的值，来自配置，不在平台层写死。"""

    voice: str
    screenshot: str
    repeat: str


@dataclass(frozen=True)
class KeyName:
    """一个规范主键在三套抓取后端里的名字（唯一键名表，后端都从这里取，不各写一份）。

    keysym：XKB/X11 keysym 名（python-xlib `XK.string_to_keysym` 与 GNOME accelerator 同名）；
    portal：GlobalShortcuts `preferred_trigger` 的主键写法——xdg-desktop-portal-gnome 50.0
    `portal_trigger_to_settings` 只换修饰键、主键原样拼进 GTK accelerator，所以必须是 keysym 名
    （"`" 在 Gdk/xkbcommon 中都解析不到，故 grave 写 "grave"）；evdev：`evdev.ecodes` 属性名。
    """

    keysym: str
    portal: str
    evdev: str


def _build_key_table() -> dict[str, KeyName]:
    table: dict[str, KeyName] = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        table[c] = KeyName(keysym=c, portal=c.upper(), evdev=f"KEY_{c.upper()}")
    for d in "0123456789":
        table[d] = KeyName(keysym=d, portal=d, evdev=f"KEY_{d}")
    for i in range(1, 13):
        table[f"f{i}"] = KeyName(keysym=f"F{i}", portal=f"F{i}", evdev=f"KEY_F{i}")
    for name, keysym, portal, evdev in (
        ("grave", "grave", "grave", "KEY_GRAVE"),
        ("space", "space", "space", "KEY_SPACE"),
        ("tab", "Tab", "Tab", "KEY_TAB"),
        ("enter", "Return", "Return", "KEY_ENTER"),
        ("escape", "Escape", "Escape", "KEY_ESC"),
        ("backspace", "BackSpace", "BackSpace", "KEY_BACKSPACE"),
        ("delete", "Delete", "Delete", "KEY_DELETE"),
        ("minus", "minus", "minus", "KEY_MINUS"),
        ("equal", "equal", "equal", "KEY_EQUAL"),
        ("semicolon", "semicolon", "semicolon", "KEY_SEMICOLON"),
        ("comma", "comma", "comma", "KEY_COMMA"),
        ("dot", "period", "period", "KEY_DOT"),
        ("slash", "slash", "slash", "KEY_SLASH"),
        ("backslash", "backslash", "backslash", "KEY_BACKSLASH"),
    ):
        table[name] = KeyName(keysym=keysym, portal=portal, evdev=evdev)
    return table


# 规范主键名 → 各后端名字。含旧配置可能用到的键（escape/minus 等），抓取后端照常解析；
# 设置写入允许的子集见 HOTKEY_MAIN_KEYS。
KEY_TABLE = _build_key_table()
KEY_ALIASES = {"`": "grave", "return": "enter", "esc": "escape", "period": "dot"}
for _name, _key in KEY_TABLE.items():  # keysym 名（小写）→ 规范名，如 backspace、f1、return
    KEY_ALIASES.setdefault(_key.keysym.lower(), _name)


def canonical_key(name: str) -> str:
    """主键名规范化（大小写、`/return/esc/period 等别名）。不认识的名字原样小写返回。"""
    key = name.strip().lower()
    return key if key in KEY_TABLE else KEY_ALIASES.get(key, key)


def normalize_hotkey(hotkey: str) -> str:
    parts = [item.strip().lower() for item in hotkey.replace("-", "+").split("+") if item.strip()]
    mapped = [MOD_ALIASES.get(item, item) for item in parts]
    if mapped:
        mapped[-1] = canonical_key(mapped[-1])
    return "+".join(mapped)


# 设置写入的唯一规则（批准计划 L1）：主键 = a-z、0-9、grave、space、tab、enter、F1–F12，
# 三套后端名字都取自 KEY_TABLE。
HOTKEY_MODIFIERS = ("ctrl", "alt", "shift", "super")
HOTKEY_MAIN_KEYS = frozenset(
    [chr(c) for c in range(ord("a"), ord("z") + 1)]
    + [str(i) for i in range(10)]
    + ["grave", "space", "tab", "enter"]
    + [f"f{i}" for i in range(1, 13)]
)
assert HOTKEY_MAIN_KEYS <= KEY_TABLE.keys()


def validate_hotkey(hotkey: str) -> str:
    """规范化并校验一条快捷键，返回规范串（修饰键按 ctrl/alt/shift/super 顺序 + 主键）。

    规则：至少一个修饰键（ctrl/alt/shift/super 任一，含仅 shift——会占用该键的普通输入，属用户可选，
    提醒由界面给出）；修饰键不重复；恰好一个主键且在 HOTKEY_MAIN_KEYS 内。
    不合法抛 LogicError(invalid_request)，message 写明收到什么，details={"kind": …, "value": 原值}，
    kind ∈ empty | modifier_only | unknown_modifier | duplicate_modifier | no_modifier | unsupported_key。
    """
    from _contracts import ErrorCode, LogicError

    kind = ""

    def bad(why: str) -> LogicError:
        return LogicError(ErrorCode.invalid_request, f"快捷键 {hotkey!r} 不可用：{why}", {"kind": kind, "value": str(hotkey)})

    parts = normalize_hotkey(str(hotkey)).split("+") if str(hotkey).strip() else []
    if not parts or parts == [""]:
        kind = "empty"
        raise bad("为空")
    *mods, main = parts
    if main in HOTKEY_MODIFIERS:
        kind = "modifier_only"
        raise bad("只有修饰键，缺少主键")
    unknown = [m for m in mods if m not in HOTKEY_MODIFIERS]
    if unknown:
        kind = "unknown_modifier"
        raise bad(f"{'+'.join(unknown)} 不是修饰键（只支持 ctrl/alt/shift/super），或包含多个主键")
    if len(set(mods)) != len(mods):
        kind = "duplicate_modifier"
        raise bad("修饰键重复")
    if not mods:
        kind = "no_modifier"
        raise bad("至少需要一个修饰键（Ctrl/Alt/Shift/Super）")
    if main not in HOTKEY_MAIN_KEYS:
        kind = "unsupported_key"
        raise bad(f"不支持主键 {main!r}（支持 a-z、0-9、grave、space、tab、enter、f1-f12）")
    ordered = [m for m in HOTKEY_MODIFIERS if m in mods]
    return "+".join(ordered + [main])


def ensure_distinct_hotkeys(hotkeys: dict[str, str]) -> None:
    """三条快捷键规范化后不得重复；值须已经过 validate_hotkey。重复抛 LogicError(invalid_request)。"""
    from _contracts import ErrorCode, LogicError

    by_value: dict[str, list[str]] = {}
    for name, value in hotkeys.items():
        by_value.setdefault(value, []).append(name)
    dup = {value: names for value, names in by_value.items() if len(names) > 1}
    if dup:
        desc = "；".join(f"{'、'.join(names)} 都是 {value}" for value, names in dup.items())
        listing = "，".join(f"{name}={value}" for name, value in hotkeys.items())
        raise LogicError(ErrorCode.invalid_request, f"快捷键重复：{desc}（当前 {listing}）")


def format_hotkey(hotkey_str: str) -> str:
    """将配置字符串格式化为用户友好的显示文本（Ctrl+Alt+Z）。"""
    parts = [p.strip() for p in hotkey_str.lower().split("+") if p.strip()]
    if not parts:
        return hotkey_str
    mod_map = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super"}
    mods = [mod_map.get(p, p.capitalize()) for p in parts[:-1]]
    key = parts[-1]
    if key == "grave":
        key = "`"
    elif len(key) == 1:
        key = key.upper()
    else:
        key = key.capitalize()
    return "+".join(mods + [key])


# xdg-desktop-portal-gnome 50.0 src/globalshortcuts.c portal_trigger_to_settings 只认
# CTRL/SHIFT/ALT/NUM/LOGO（XDG shortcuts 规范），没有 SUPER。
PORTAL_MODIFIERS = {"ctrl": "CTRL", "alt": "ALT", "shift": "SHIFT", "super": "LOGO"}


def to_portal_trigger(hotkey: str) -> str:
    """ctrl+alt+z → CTRL+ALT+Z；super → LOGO；主键取 KEY_TABLE 的 portal 名（keysym 名）。"""
    parts = [p.strip() for p in hotkey.lower().split("+") if p.strip()]
    if not parts:
        return hotkey
    mods = [PORTAL_MODIFIERS.get(p, p.upper()) for p in parts[:-1]]
    key = canonical_key(parts[-1])
    name = KEY_TABLE.get(key)
    return "+".join(mods + [name.portal if name else (key.upper() if len(key) == 1 else key)])


def normalize_accel(accel: str) -> str:
    """把 GNOME 的 <Primary><Alt>d 形式归一化成 ctrl+alt+d。"""
    s = accel.strip().lower()
    for token, repl in (
        ("<primary>", "ctrl+"),
        ("<control>", "ctrl+"),
        ("<alt>", "alt+"),
        ("<shift>", "shift+"),
        ("<super>", "super+"),
        ("<mod1>", "alt+"),
        ("<mod4>", "super+"),
    ):
        s = s.replace(token, repl)
    s = s.replace("<", "").replace(">", "")
    parts = [p for p in s.split("+") if p]
    if not parts:
        return ""
    parts[-1] = canonical_key(parts[-1])
    return "+".join(parts)


def parse_hotkey_evdev(hotkey_str: str) -> dict:
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

    modifiers = []
    for mod in parts[:-1]:
        if mod in mod_map:
            modifiers.append(mod_map[mod])

    trigger_name = parts[-1]
    name = KEY_TABLE.get(canonical_key(trigger_name))
    if name is None:
        raise ValueError(f"Unknown evdev key: {trigger_name}")
    trigger = getattr(ecodes, name.evdev)

    return {"trigger": trigger, "modifiers": modifiers}
