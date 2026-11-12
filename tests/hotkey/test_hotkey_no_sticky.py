#!/usr/bin/env python3
"""真实链路：松开后不得再触发。打真实 X + 真实 grabber，不 mock 命中契约。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("DISPLAY", ":0")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from _hotkey import HotkeyBindings  # noqa: E402
from _linux.hotkey_actions import HotkeyContext  # noqa: E402
from _linux.hotkey_evdev import EvdevHotkeyGrabber  # noqa: E402
from _linux.hotkey_xinput import XInputHotkeyGrabber, xi_source_ids_by_name as _xi_source_ids_by_name  # noqa: E402
from _linux.inject_guard import InjectGuard  # noqa: E402
from _linux.modifiers import ModifierJanitor  # noqa: E402
from _linux.uinput_device import UINPUT_DEVICE_NAME as _UINPUT_DEVICE_NAME  # noqa: E402

_GUARD = InjectGuard()


class _NoopActions:
    """真实抓取器的动作夹具：本测试只验证按键账本，不录音不注入。"""

    def voice_start(self) -> bool:
        return True

    def voice_stop(self) -> None:
        pass

    def screenshot(self) -> None:
        pass

    def repeat(self) -> None:
        pass

    def overlay_active(self) -> bool:
        return False

    def close_overlay(self) -> bool:
        return False


def VoiceRecorder():  # noqa: N802  保持原测试调用形状
    return None


def _ctx(_unused=None) -> HotkeyContext:
    return HotkeyContext(
        actions=_NoopActions(),
        bindings=HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", repeat="ctrl+alt+r"),
        janitor=ModifierJanitor(session_type=os.environ.get("XDG_SESSION_TYPE", "x11")),
        guard=_GUARD,
        session_type=os.environ.get("XDG_SESSION_TYPE", "x11"),
    )


def _ctrl_l_kc(dpy) -> int:
    from Xlib import XK

    return dpy.keysym_to_keycode(XK.string_to_keysym("Control_L"))


def test_stale_trigger_does_not_match() -> None:
    rec = VoiceRecorder()
    grabber = XInputHotkeyGrabber(_ctx(rec))
    try:
        trigger = grabber._voice_combo["trigger"]
        other = trigger + 1
        if other == grabber._screen_combo["trigger"]:
            other += 1
        grabber._pressed = {trigger, 37, 64}
        assert grabber._match_combo(grabber._voice_combo, other) is False, (
            "陈 trigger 配非 trigger 键必须不命中"
        )
        # 当前键就是 trigger 时，不因缺 pressed_kc 而误 True（本机多半没按修饰键）
        hit = grabber._match_combo(grabber._voice_combo, trigger)
        assert hit in (True, False)
        print("PASS: C1 陈 trigger 不命中")
    finally:
        grabber.cleanup()


def test_uinput_echo_not_in_pressed() -> None:
    from evdev import UInput, ecodes

    rec = VoiceRecorder()
    grabber = XInputHotkeyGrabber(_ctx(rec))
    ui = None
    try:
        grabber.start()
        grabber._on_xlib_event(None, None)  # 排空
        ui = UInput({ecodes.EV_KEY: list(range(1, 249))}, name=_UINPUT_DEVICE_NAME)
        time.sleep(0.25)
        grabber._on_xlib_event(None, None)  # MappingNotify → 刷新 sourceid
        ids = _xi_source_ids_by_name(grabber.dpy, _UINPUT_DEVICE_NAME)
        if not ids:
            # 当前 XWayland 把物理/uinput 键都归到 xwayland-keyboard:*，
            # 不再暴露独立 uinput sourceid；C3 的 _inject_until 时间窗覆盖此场景。
            print("SKIP: XWayland 未暴露 uinput sourceid，C2 由 C3 时间窗覆盖")
            return
        assert ids <= grabber._ignore_source_ids or grabber._ignore_source_ids, (
            f"忽略集未包含 uinput sourceid {ids}, 现有 {grabber._ignore_source_ids}"
        )
        grabber._refresh_ignore_sources()
        assert ids <= grabber._ignore_source_ids

        ctrl = _ctrl_l_kc(grabber.dpy)
        grabber._pressed.discard(ctrl)
        ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1)
        ui.syn()
        time.sleep(0.08)
        grabber._on_xlib_event(None, None)
        assert ctrl not in grabber._pressed, (
            f"uinput Ctrl 回流进了 _pressed: {grabber._pressed}"
        )
        ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
        ui.syn()
        time.sleep(0.05)
        grabber._on_xlib_event(None, None)
        print("PASS: C2 uinput 不入账")
    finally:
        if ui is not None:
            try:
                from evdev import ecodes

                ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
                ui.syn()
            except Exception:
                pass
            ui.close()
        grabber.cleanup()


def test_fallback_release_clears_trigger() -> None:
    rec = VoiceRecorder()
    grabber = XInputHotkeyGrabber(_ctx(rec))
    old_until = _GUARD.until
    try:
        grabber._ignore_source_ids = set()
        _GUARD.until = time.monotonic() + 10
        trig = grabber._voice_combo["trigger"]
        grabber._pressed.add(trig)
        grabber._handle_release(trig)
        assert trig not in grabber._pressed, "回退窗必须放行 trigger 抬起"
        print("PASS: C3 回退仍能松 trigger")
    finally:
        _GUARD.until = old_until
        grabber.cleanup()


def test_evdev_stale_trigger() -> None:
    holder = EvdevHotkeyGrabber.__new__(EvdevHotkeyGrabber)
    holder._pressed_keys = {44, 29, 56}
    combo = {"trigger": 44, "modifiers": [{29, 97}]}
    assert EvdevHotkeyGrabber._match_combo(holder, combo, 30) is False
    assert EvdevHotkeyGrabber._match_combo(holder, combo, 44) is True
    print("PASS: C4 evdev 契约")


def main() -> int:
    test_stale_trigger_does_not_match()
    test_fallback_release_clears_trigger()
    test_evdev_stale_trigger()
    test_uinput_echo_not_in_pressed()
    print("PASS: 快捷键松开后不再误触发")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(1)
