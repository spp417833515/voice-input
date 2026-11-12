"""截图采集回调的形状（不导入 GTK，业务层可直接引用）。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

Rect = tuple[int, int, int, int]


@dataclass
class CaptureCallbacks:
    on_stage: Callable[[str], None]
    on_captured: Callable[[bytes, Rect], None]
    on_cancelled: Callable[[], None]
    on_failed: Callable[[str], None]
