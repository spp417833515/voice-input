#!/usr/bin/env python3
"""纯函数/动作层测试：不打开选区、不争抢真实热键。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 避免 pynput 在导入阶段尝试连 X；本测试不需要真实显示。
os.environ.setdefault("DISPLAY", "")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import shutil  # noqa: E402

from _hotkey import HotkeyBindings, normalize_accel, to_portal_trigger  # noqa: E402
from _linux.conflicts import scan_hotkey_conflicts  # noqa: E402
from _linux.geometry import selection_crop_box  # noqa: E402
from _linux.hotkey_actions import HotkeyActionMixin, HotkeyContext  # noqa: E402
from _linux.hotkey_portal import PortalGlobalShortcutsGrabber  # noqa: E402
from _linux.inject_guard import InjectGuard  # noqa: E402

DEFAULTS = HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", repeat="ctrl+alt+r")


def test_portal_trigger_format() -> None:
    assert to_portal_trigger("ctrl+alt+z") == "CTRL+ALT+Z"
    assert to_portal_trigger("ctrl+alt+x") == "CTRL+ALT+X"
    assert to_portal_trigger("ctrl+grave") == "CTRL+grave"
    print("PASS: portal trigger 格式")


def test_accel_normalize() -> None:
    assert normalize_accel("<Primary><Alt>d") == "ctrl+alt+d"
    assert normalize_accel("<Control><Shift>z") == "ctrl+shift+z"
    assert normalize_accel("<Super>d") == "super+d"
    print("PASS: GNOME accelerator 归一化")


def test_selection_crop_box() -> None:
    assert selection_crop_box(0, 0, 1.0, 1.0, 200, 200, 400, 200) == (
        200, 200, 600, 400
    )
    assert selection_crop_box(0, 0, 2.0, 2.0, 100, 100, 200, 100) == (
        200, 200, 600, 400
    )
    # 副屏偏移：mon_img_x/y 是图像内偏移，不是屏幕逻辑偏移
    assert selection_crop_box(1920, 0, 1.0, 1.0, 10, 20, 30, 40) == (
        1930, 20, 1960, 60
    )
    print("PASS: 选区 → 图像 crop 坐标")


def test_parse_shortcuts_description() -> None:
    grabber = PortalGlobalShortcutsGrabber.__new__(PortalGlobalShortcutsGrabber)
    raw = [
        ("voice", {"trigger_description": "按 <Control><Alt>z"}),
        ("screenshot", {"trigger": "CTRL+ALT+X"}),
        ("repeat", {"preferred_trigger": "CTRL+ALT+R"}),
    ]
    parsed = grabber._parse_shortcuts(raw)
    assert parsed["voice"] == "按 <Control><Alt>z"
    assert parsed["screenshot"] == "CTRL+ALT+X"
    assert parsed["repeat"] == "CTRL+ALT+R"
    print("PASS: portal shortcuts 响应解析")


class _DummyRecorder:
    """HotkeyActions 的录音部分夹具（voice_start/voice_stop 计数）。"""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def voice_start(self) -> bool:
        self.starts += 1
        return True

    def voice_stop(self) -> None:
        self.stops += 1

    def screenshot(self) -> None:
        pass

    def repeat(self) -> None:
        pass

    def overlay_active(self) -> bool:
        return False

    def close_overlay(self) -> bool:
        return False


class _DummyActions(HotkeyActionMixin):
    _release_modifiers_on_stop = False

    def __init__(self) -> None:
        self.recorder = _DummyRecorder()
        self._init_hotkey_actions(
            HotkeyContext(
                actions=self.recorder,
                bindings=DEFAULTS,
                janitor=None,
                guard=InjectGuard(),
                session_type="x11",
            )
        )


def test_action_mixin_voice_start_stop() -> None:
    actions = _DummyActions()
    actions._on_voice_press()
    actions._on_voice_press()  # 幂等：不应重复 start
    assert actions.voice_active is True
    assert actions.recorder.starts == 1
    actions._do_release()
    actions._do_release()
    assert actions.voice_active is False
    assert actions.recorder.stops == 1
    print("PASS: 动作层 PTT 幂等")


def test_conflict_scanner_known_show_desktop() -> None:
    if not shutil.which("gsettings"):
        print("SKIP: 无 gsettings")
        return
    conflicts = scan_hotkey_conflicts(HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+d", repeat="ctrl+alt+r")).conflicts
    assert any("show-desktop" in item for item in conflicts), conflicts
    print("PASS: 冲突扫描识别 show-desktop")


def main() -> int:
    test_portal_trigger_format()
    test_accel_normalize()
    test_selection_crop_box()
    test_parse_shortcuts_description()
    test_action_mixin_voice_start_stop()
    test_conflict_scanner_known_show_desktop()
    print("PASS: portal / 动作层纯函数测试")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
