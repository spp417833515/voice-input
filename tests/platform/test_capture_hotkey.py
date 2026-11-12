from __future__ import annotations

from _capture import FakeAudioSource, SessionCapture, chunk_pcm, chunk_width, duration_s
from _focus import StaticFocusProbe
from _hotkey import normalize_accel, to_portal_trigger
from _linux.inject_guard import InjectGuard
from _linux.watchdog import IdleSafetyWatchdog


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0  # monotonic 不会是 0；0 表示“未记录开始时刻”

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def test_short_utterance_discarded():
    # 0.1s of s16le mono 16k
    pcm = b"\x00\x00" * 1600
    cap = SessionCapture(FakeAudioSource(pcm), StaticFocusProbe(3))
    cap.start()
    result = cap.finish()
    assert result.accepted is False
    assert result.reason == "too_short"
    assert result.focus is not None and result.focus.focus_id == 3


def test_chunks_stream_in_order_with_tail():
    chunks: list[tuple[int, int]] = []
    source = FakeAudioSource(b"\x01\x00" * 4000)  # 0.25s tail pushed at stop
    cap = SessionCapture(source, StaticFocusProbe(5), on_chunk=lambda i, b: chunks.append((i, len(b))))
    cap.start()
    width = chunk_width()  # 默认步长与 sidecar 解码步长同源（runtimes.asr.protocol）
    source.push(b"\x01\x00" * width)  # 2 个整块（字节数 = 2 × width/2 样本）
    assert chunks == [(0, width), (1, width)]
    result = cap.finish()
    assert result.accepted and result.chunks == 3
    assert chunks[-1] == (2, 8000)
    cap._on_pcm(b"\x01\x00" * width)
    assert len(chunks) == 3


def test_mic_open_failure_raises_and_resets():
    class Broken(FakeAudioSource):
        def start(self, sink):
            raise OSError("no device")

    cap = SessionCapture(Broken(), StaticFocusProbe(1))
    try:
        cap.start()
    except OSError:
        pass
    else:
        raise AssertionError("开麦失败必须抛出")
    assert cap.recording is False


class FakeManager:
    def __init__(self, clock) -> None:
        self.voice_active = True
        self.voice_started_at = clock()
        self.stopped: list[str] = []

    def force_stop_voice(self) -> None:
        self.voice_active = False
        self.stopped.append("finish")


class FakeJanitor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def clear_uinput(self):
        self.calls.append("uinput")

    def release_seat_grab(self):
        self.calls.append("seat")

    def clear_xtest(self):
        self.calls.append("xtest")


def test_watchdog_finishes_not_cancels_after_120s_and_feeds_systemd():
    clock = Clock()
    manager = FakeManager(clock)
    janitor = FakeJanitor()
    notes: list[str] = []
    wd = IdleSafetyWatchdog(
        manager=manager, janitor=janitor, guard=InjectGuard(), overlay_active=lambda: False,
        notify=notes.append, clock=clock,
    )
    clock.advance(119)
    assert wd.tick() is True
    assert manager.stopped == [] and janitor.calls == []
    clock.advance(2)
    assert wd.tick() is True
    assert manager.stopped == ["finish"]
    assert janitor.calls == ["uinput", "seat", "xtest"]
    assert notes == ["WATCHDOG=1", "WATCHDOG=1"]


def test_watchdog_skips_cleanup_while_injecting_or_overlay():
    clock = Clock()
    manager = FakeManager(clock)
    manager.voice_active = False
    janitor = FakeJanitor()
    guard = InjectGuard()
    wd = IdleSafetyWatchdog(manager=manager, janitor=janitor, guard=guard, overlay_active=lambda: True, notify=lambda s: None, clock=clock)
    wd.tick()
    assert janitor.calls == []
    wd.overlay_active = lambda: False
    import threading

    held = threading.Event()
    release = threading.Event()

    def hold():
        with guard.lock:
            held.set()
            release.wait(2)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(2)
    wd.tick()
    release.set()
    t.join()
    assert janitor.calls == []
    wd.tick()
    assert janitor.calls == ["uinput", "seat", "xtest"]


def test_hotkey_helpers():
    assert to_portal_trigger("ctrl+alt+z") == "CTRL+ALT+Z"
    assert to_portal_trigger("ctrl+grave") == "CTRL+grave"
    assert normalize_accel("<Primary><Alt>d") == "ctrl+alt+d"
    assert duration_s(b"\x00\x00" * 8000) == 0.5
    assert len(chunk_pcm(b"\x00\x00" * 16000, chunk_s=0.5)) == 2


def test_validated_main_keys_parse_in_every_capture_backend():
    """每个允许主键（含 enter、f1-f12）× 修饰键组合（含仅 shift、super）：三后端都取 KEY_TABLE 同一名字。
    portal 主键名与 X11 keysym 名用 libxkbcommon 区分大小写查名（纯查表，不连显示、不注册）；
    evdev / python-xlib 在装了对应库的解释器里实际解析（项目 .venv 有，隔离 venv-test 没有则跳过该段）。"""
    import ctypes
    import ctypes.util
    import importlib.util

    from _hotkey import HOTKEY_MAIN_KEYS, KEY_TABLE, PORTAL_MODIFIERS, validate_hotkey

    combos = ("ctrl+alt", "shift", "super", "ctrl+shift", "alt+super")
    lib = ctypes.util.find_library("xkbcommon")
    xkb = None
    if lib:
        xkb = ctypes.CDLL(lib).xkb_keysym_from_name
        xkb.restype, xkb.argtypes = ctypes.c_uint32, [ctypes.c_char_p, ctypes.c_int]
    evdev = importlib.util.find_spec("evdev") is not None
    xlib = importlib.util.find_spec("Xlib") is not None
    if evdev:
        from evdev import ecodes

        from _hotkey import parse_hotkey_evdev
    if xlib:
        from Xlib import XK

        from _linux.x11keys import parse_hotkey
    assert {"enter", "f1", "f12", "grave", "space", "tab"} <= HOTKEY_MAIN_KEYS
    for key in sorted(HOTKEY_MAIN_KEYS):
        name = KEY_TABLE[key]
        if xkb is not None:
            assert xkb(name.portal.encode(), 0) != 0, (key, name.portal)
            assert xkb(name.keysym.encode(), 0) != 0, (key, name.keysym)
        for mods in combos:
            hotkey = validate_hotkey(f"{mods}+{key}")
            expected_mods = [PORTAL_MODIFIERS[m] for m in hotkey.split("+")[:-1]]
            assert to_portal_trigger(hotkey) == "+".join([*expected_mods, name.portal])
            gnome = {"ctrl": "<Control>", "alt": "<Alt>", "shift": "<Shift>", "super": "<Super>"}  # gsettings 实存形式
            assert normalize_accel("".join(gnome[m] for m in hotkey.split("+")[:-1]) + name.keysym) == hotkey
            if evdev:
                assert parse_hotkey_evdev(hotkey)["trigger"] == getattr(ecodes, name.evdev)
            if xlib:
                assert parse_hotkey(hotkey)[1] == XK.string_to_keysym(name.keysym) != 0, key
