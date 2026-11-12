"""官方模型 / 长音频 / 4b 仍为 opt-in，默认闸门必须跳过，不得报绿。"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SIDECAR_OFFICIAL") != "1",
    reason="official model / long-audio / 4b 仍未达标，默认闸门跳过，不得报绿",
)


def test_official_long_audio_and_4b_not_in_default_gate():
    raise AssertionError("若未设 SIDECAR_OFFICIAL，本测试应被跳过；不得在默认闸门运行真实模型")
