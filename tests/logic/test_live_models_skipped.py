"""真实模型链路：4b 未下载、长录音未达标时必须显式 skip，不得报绿。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from _asr import AsrSidecarClient
from _contracts import ASR_STREAMING_BLOCKERS, SIDECAR_DEFAULT_BASE_URL
from _ollama import DEFAULT_OLLAMA_BASE_URL, OllamaClient

ROOT = Path(__file__).resolve().parents[2]


def _4b_present() -> tuple[bool, str]:
    url = os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=2.0)
    except httpx.HTTPError as exc:
        return False, f"无法连接 Ollama ({url}): {exc}"
    if resp.status_code != 200:
        return False, f"Ollama /api/tags HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return False, "Ollama /api/tags 不是合法 JSON"
    names = [m.get("name", "") for m in data.get("models") or []]
    if any(name == "qwen3.5:4b" or name.startswith("qwen3.5:4b") for name in names):
        return True, "present"
    return False, "qwen3.5:4b 未落盘（fake-ip 阻塞官方 pull，本层不改系统代理/DNS/Ollama）"


def test_real_qwen35_4b_skipped_until_downloaded():
    present, reason = _4b_present()
    if not present:
        pytest.skip(reason)
    # 即使将来已下载，本测试也不在 L1 宣称产品验收通过；仅做一次最小 think:false 探活。
    with OllamaClient(timeout_s=30) as client:
        text = client.chat(
            model="qwen3.5:4b",
            messages=[{"role": "user", "content": "只回复：ok"}],
            think=False,
        )
    assert text


def test_real_asr_sidecar_not_claimed_green_for_long_audio():
    """若 sidecar 未在监听则 skip；即使可连，也不得把长录音路径标为通过。"""
    client = AsrSidecarClient(base_url=SIDECAR_DEFAULT_BASE_URL, timeout_s=2)
    try:
        try:
            client.create_session()
        except Exception as exc:
            pytest.skip(f"ASR sidecar 未在 {SIDECAR_DEFAULT_BASE_URL} 监听或不可用: {exc}")
    finally:
        client.close()
    pytest.skip(
        "sidecar 可连，但长录音 0.5–1s / 120s 路径尚未达标，L1 不跑长录音、不报绿: "
        + " ".join(ASR_STREAMING_BLOCKERS)
    )
