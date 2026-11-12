"""Sidecar 协议与 `_asr.AsrSidecarClient` 对齐的 mock 测试，不加载官方引擎。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from _asr import AsrSidecarClient
from _contracts import ASR_STREAMING_BLOCKERS, RecognizeSessionCreate, SessionClosedReason, SessionFinalEvent
from runtimes.asr.protocol import OFFICIAL_PARTIAL_POLICY, SIDECAR_SESSIONS_PATH, SIDECAR_TRANSCRIBE_PATH
from tests.sidecar.fake_engine import FakeEngine


def _sidecar(app) -> AsrSidecarClient:
    return AsrSidecarClient(base_url="http://127.0.0.1:17946", client=TestClient(app))


def test_health_lists_blockers_and_partial_policy(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["unmet_blockers"] == list(ASR_STREAMING_BLOCKERS)
    assert "tail_text" in body["partial_policy"]
    assert OFFICIAL_PARTIAL_POLICY


def test_client_transcribe_and_stream_match_contracts(app, engine):
    sidecar = _sidecar(app)
    pcm = bytes([1, 0]) * 8
    batch = sidecar.transcribe(pcm, language="zh", filename="audio.pcm")
    assert batch.ok is True
    assert isinstance(batch.text, str)
    created = sidecar.create_session(RecognizeSessionCreate(language="zh"))
    accepted = sidecar.send_chunk(created.session_id, 0, bytes([1, 0]) * 800)
    assert accepted.accepted is True
    assert accepted.index == 0
    final = sidecar.finish(created.session_id)
    assert isinstance(final, SessionFinalEvent)
    assert final.text == engine.text
    assert created.session_id in engine.finished
    assert not engine.has_stream(created.session_id)


def test_cancel_does_not_finish(app, engine):
    sidecar = _sidecar(app)
    created = sidecar.create_session()
    sidecar.send_chunk(created.session_id, 0, bytes([0, 0]) * 10)
    closed = sidecar.cancel(created.session_id)
    assert closed.reason == SessionClosedReason.cancelled
    assert created.session_id in engine.cancelled
    assert created.session_id not in engine.finished


def test_second_session_conflicts(app):
    sidecar = _sidecar(app)
    first = sidecar.create_session()
    try:
        sidecar.create_session()
        raise AssertionError("expected conflict")
    except Exception as exc:
        assert "409" in str(exc) or "conflict" in str(exc).lower() or "单会话" in str(exc)
    sidecar.cancel(first.session_id)


def test_partial_keeps_stable_empty():
    other = FakeEngine()
    other.start_stream("x")
    partial = other.push_pcm("x", bytes([0, 0]))
    assert partial is not None
    assert partial.stable_text == ""
    assert partial.tail_text
    assert SIDECAR_TRANSCRIBE_PATH == "/v1/transcribe"
    assert SIDECAR_SESSIONS_PATH == "/v1/sessions"


def test_oversize_stream_is_rejected(engine, app):
    engine.max_session_bytes = 8
    sidecar = _sidecar(app)
    created = sidecar.create_session()
    try:
        sidecar.send_chunk(created.session_id, 0, bytes([0, 0]) * 16)
        raise AssertionError("expected invalid request")
    except Exception as exc:
        assert "400" in str(exc) or "上限" in str(exc)
    sidecar.cancel(created.session_id)


def test_importing_sidecar_package_does_not_load_torch():
    import sys

    for name in list(sys.modules):
        if name == "torch" or name.startswith("torch.") or name == "vllm" or name.startswith("vllm."):
            raise AssertionError("unexpected runtime import: " + name)
    import runtimes.asr
    import runtimes.asr.server

    assert "torch" not in sys.modules
    assert "vllm" not in sys.modules
    assert runtimes.asr.OFFICIAL_PARTIAL_POLICY
