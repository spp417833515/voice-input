"""把桌面流程挂到快捷键动作接口（`_linux.hotkey_actions.HotkeyActions`）。"""
from __future__ import annotations

import threading

from _desktop.delivery import RepeatAction
from _desktop.screenshot import ScreenshotFlow
from _desktop.voice import VoiceFlow


class DesktopActions:
    def __init__(self, *, voice: VoiceFlow, screenshot: ScreenshotFlow, repeat: RepeatAction, spawn=None) -> None:
        self.voice = voice
        self.shot = screenshot
        self.repeat_action = repeat
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())

    def voice_start(self) -> bool:
        return self.voice.start()

    def voice_stop(self) -> None:
        self.voice.stop()

    def screenshot(self) -> None:
        self.shot.trigger()

    def repeat(self) -> None:
        self._spawn(self.repeat_action.run)

    def overlay_active(self) -> bool:
        return self.shot.overlay_active()

    def close_overlay(self) -> bool:
        return self.shot.close_overlay()
