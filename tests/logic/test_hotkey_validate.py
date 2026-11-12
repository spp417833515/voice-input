"""L1：快捷键写入的唯一校验规则（_hotkey.validate_hotkey / ensure_distinct_hotkeys 经 _config.apply_config_update）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _config import ConfigStore, apply_config_update
from _contracts import AppConfigUpdate, ErrorCode, LogicError
from _hotkey import HOTKEY_MAIN_KEYS, ensure_distinct_hotkeys, validate_hotkey


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ctrl+alt+z", "ctrl+alt+z"),
        ("alt+x", "alt+x"),  # 用户现有配置（05:51:30 保存）保持合法、不改写
        ("ctrl+alt+r", "ctrl+alt+r"),
        ("Control+Alt+Z", "ctrl+alt+z"),
        ("alt+ctrl+x", "ctrl+alt+x"),
        ("ctrl+grave", "ctrl+grave"),
        ("ctrl+shift+z", "ctrl+shift+z"),
        ("super+space", "super+space"),
        ("Primary-Alt-1", "ctrl+alt+1"),
        ("cmd+tab", "super+tab"),
        ("ctrl+alt+f1", "ctrl+alt+f1"),
        ("Ctrl+Alt+F12", "ctrl+alt+f12"),
        ("ctrl+alt+enter", "ctrl+alt+enter"),
        ("ctrl+alt+Return", "ctrl+alt+enter"),
        ("ctrl+`", "ctrl+grave"),
        ("shift+a", "shift+a"),  # 仅 shift：用户可选（会占用该键的普通输入，提醒由界面给出）
        ("Shift+F5", "shift+f5"),
        ("shift+enter", "shift+enter"),
    ],
)
def test_valid_hotkeys_are_canonical(raw: str, expected: str):
    assert validate_hotkey(raw) == expected


@pytest.mark.parametrize(
    ("raw", "kind", "why"),
    [
        ("", "empty", "为空"),
        ("   ", "empty", "为空"),
        ("z", "no_modifier", "至少需要一个修饰键"),
        ("f1", "no_modifier", "至少需要一个修饰键"),
        ("ctrl", "modifier_only", "只有修饰键"),
        ("shift", "modifier_only", "只有修饰键"),
        ("ctrl+alt", "modifier_only", "只有修饰键"),
        ("ctrl+shift", "modifier_only", "只有修饰键"),
        ("ctrl+ctrl+z", "duplicate_modifier", "修饰键重复"),
        ("shift+shift+a", "duplicate_modifier", "修饰键重复"),
        ("ctrl+a+b", "unknown_modifier", "不是修饰键"),
        ("hyper+z", "unknown_modifier", "不是修饰键"),
        ("ctrl+alt+f13", "unsupported_key", "不支持主键"),
        ("ctrl+alt+dot", "unsupported_key", "不支持主键"),
        ("ctrl+alt+escape", "unsupported_key", "不支持主键"),
        ("ctrl+alt+é", "unsupported_key", "不支持主键"),
    ],
)
def test_invalid_hotkeys_rejected_with_kind_and_reason(raw: str, kind: str, why: str):
    with pytest.raises(LogicError) as info:
        validate_hotkey(raw)
    assert info.value.code == ErrorCode.invalid_request
    assert info.value.details == {"kind": kind, "value": raw}
    assert why in info.value.message and repr(raw) in info.value.message


def test_main_key_set_is_the_approved_plan_set():
    """批准计划 L1：a-z、0-9、grave、space、tab、enter、F1–F12。"""
    assert HOTKEY_MAIN_KEYS == (
        frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
        | {"grave", "space", "tab", "enter"}
        | {f"f{i}" for i in range(1, 13)}
    )


def test_duplicates_rejected_with_all_values():
    with pytest.raises(LogicError) as info:
        ensure_distinct_hotkeys({"hotkey_voice": "ctrl+alt+z", "hotkey_screenshot": "ctrl+alt+z", "hotkey_repeat": "ctrl+alt+r"})
    msg = info.value.message
    assert info.value.code == ErrorCode.invalid_request
    assert "hotkey_voice、hotkey_screenshot 都是 ctrl+alt+z" in msg and "hotkey_repeat=ctrl+alt+r" in msg


def test_apply_update_checks_duplicates_against_stored_values(tmp_path: Path):
    path = tmp_path / ".config.json"
    path.write_text(json.dumps({"hotkey_voice": "ctrl+alt+z", "hotkey_screenshot": "alt+x", "hotkey_repeat": "ctrl+alt+r"}), encoding="utf-8")
    store = ConfigStore(path)
    before = path.read_bytes()
    with pytest.raises(LogicError) as info:
        apply_config_update(store, AppConfigUpdate(hotkey_repeat="ALT+X"))
    assert "hotkey_screenshot、hotkey_repeat 都是 alt+x" in info.value.message
    assert path.read_bytes() == before
    with pytest.raises(LogicError) as info:
        apply_config_update(store, AppConfigUpdate(hotkey_voice="ctrl"))
    assert info.value.message.startswith("hotkey_voice: ") and "只有修饰键" in info.value.message
    assert info.value.details == {"kind": "modifier_only", "value": "ctrl", "field": "hotkey_voice"}
    assert path.read_bytes() == before
    view = apply_config_update(store, AppConfigUpdate(hotkey_screenshot="Alt+Ctrl+X"))
    assert view.hotkey_screenshot == "ctrl+alt+x"
    # 交换两条：同一次更新内的新值彼此查重，不与旧值误判
    view = apply_config_update(store, AppConfigUpdate(hotkey_voice="ctrl+alt+r", hotkey_repeat="ctrl+alt+z"))
    assert (view.hotkey_voice, view.hotkey_repeat) == ("ctrl+alt+r", "ctrl+alt+z")


def test_existing_config_is_not_rewritten_on_load_or_unrelated_update(tmp_path: Path):
    path = tmp_path / ".config.json"
    path.write_text(json.dumps({"hotkey_voice": "ctrl+alt+z", "hotkey_screenshot": "alt+x", "hotkey_repeat": "ctrl+alt+r"}), encoding="utf-8")
    store = ConfigStore(path)
    view = apply_config_update(store, AppConfigUpdate(translate_target_lang="en"))
    assert (view.hotkey_voice, view.hotkey_screenshot, view.hotkey_repeat) == ("ctrl+alt+z", "alt+x", "ctrl+alt+r")
