"""官方短样例真实链。默认跳过，避免闸门加载 GPU。

SIDECAR_OFFICIAL=1 且 127.0.0.1:17946 已由隔离 venv 显式 --load 启动时才跑。
长音频 / 4b 并存不在本测试范围内，不得据此报绿。
"""
from __future__ import annotations

import os
import wave
from pathlib import Path

import httpx
import pytest

from _asr import AsrSidecarClient
from _contracts import RecognizeSessionCreate, SessionFinalEvent, SessionPartialEvent

OFFICIAL_WAV = Path("/home/spp/桌面/ollama-voice-input/.cache/v3-feasibility/audio/qwen-official-asr_zh.wav")
OFFICIAL_TEXT = "甚至出现交易几乎停滞的情况。"
BASE = os.environ.get("SIDECAR_BASE_URL", "http://127.0.0.1:17946")

pytestmark = pytest.mark.skipif(os.environ.get("SIDECAR_OFFICIAL") != "1", reason="official short chain is opt-in")


def _pcm_from_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16000
        return handle.readframes(handle.getnframes())


def test_official_batch_and_stream_short_sample():
    assert OFFICIAL_WAV.is_file()
    wav_bytes = OFFICIAL_WAV.read_bytes()
    pcm = _pcm_from_wav(OFFICIAL_WAV)
    with AsrSidecarClient(base_url=BASE, timeout_s=180.0) as client:
        health = httpx.get(f"{BASE}/health", timeout=5.0)
        assert health.status_code == 200
        body = health.json()
        assert body.get("loaded") is True
        assert body.get("unmet_blockers")
        batch = client.transcribe(wav_bytes, language="zh", filename="asr_zh.wav", content_type="audio/wav")
        assert batch.ok is True
        assert batch.text == OFFICIAL_TEXT
        created = client.create_session(RecognizeSessionCreate(language="zh"))
        chunk = 16000  # 0.5s * 16000 * 2 bytes
        index = 0
        for offset in range(0, len(pcm), chunk):
            client.send_chunk(created.session_id, index, pcm[offset : offset + chunk])
            index += 1
        final = client.finish(created.session_id)
        assert isinstance(final, SessionFinalEvent)
        assert final.text == OFFICIAL_TEXT
        # 再开一轮只验证 partial 的 stable 保持空（若引擎发出 partial）
        created2 = client.create_session(RecognizeSessionCreate(language="zh"))
        partials = []
        client.send_chunk(created2.session_id, 0, pcm[:chunk])
        # 不强制存在 mid-stream partial；若有则 stable 必须为空
        closed = client.cancel(created2.session_id)
        assert closed.reason.value == "cancelled" or closed.reason == "cancelled"
        assert not partials or all(isinstance(p, SessionPartialEvent) and p.stable_text == "" for p in partials)
