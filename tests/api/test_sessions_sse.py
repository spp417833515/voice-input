from __future__ import annotations

from pathlib import Path

from _contracts import ErrorCode, SIDECAR_CHUNK_HEADER, SessionClosedReason
from _v3_app import build_sandbox_runtime, create_app
from tests.api.conftest import TEST_TOKEN, lan_client, lan_headers, local_client, local_headers
from tests.api.providers import FixtureAsrProvider, fixture_transcribe


def _pcm(n: int = 8) -> bytes:
    return b"\x01\x00" * n


def test_chunk_idempotent_conflict_and_order(app):
    client = local_client(app)
    sid = client.post("/api/v1/recognize/sessions", json={"language": "zh"}, headers=local_headers()).json()["session_id"]
    headers = {**local_headers(), "Content-Type": "application/octet-stream"}
    first = client.post(
        f"/api/v1/recognize/sessions/{sid}/chunks",
        content=_pcm(8),
        headers={**headers, SIDECAR_CHUNK_HEADER: "0"},
    )
    assert first.status_code == 202
    again = client.post(
        f"/api/v1/recognize/sessions/{sid}/chunks",
        content=_pcm(8),
        headers={**headers, SIDECAR_CHUNK_HEADER: "0"},
    )
    assert again.status_code == 202
    different = client.post(
        f"/api/v1/recognize/sessions/{sid}/chunks",
        content=_pcm(16),
        headers={**headers, SIDECAR_CHUNK_HEADER: "0"},
    )
    assert different.status_code == 409
    skipped = client.post(
        f"/api/v1/recognize/sessions/{sid}/chunks",
        content=_pcm(8),
        headers={**headers, SIDECAR_CHUNK_HEADER: "2"},
    )
    assert skipped.status_code == 409
    odd = client.post(
        f"/api/v1/recognize/sessions/{sid}/chunks",
        content=b"\x01",
        headers={**headers, SIDECAR_CHUNK_HEADER: "1"},
    )
    assert odd.status_code == 400


def test_finish_emits_final_then_closed_and_cancel_does_not_enter_business(app, runtime):
    client = local_client(app)
    sid = client.post("/api/v1/recognize/sessions", json={"language": "zh"}, headers=local_headers()).json()["session_id"]
    client.post(
        f"/api/v1/recognize/sessions/{sid}/chunks",
        content=_pcm(),
        headers={**local_headers(), SIDECAR_CHUNK_HEADER: "0", "Content-Type": "application/octet-stream"},
    )
    from _contracts import SessionClosedEvent, SessionFinalEvent
    from _v3.http import format_sse

    events = []
    q = client.app.state.sessions.subscribe(sid)
    finish = client.post(f"/api/v1/recognize/sessions/{sid}/finish", headers=local_headers())
    assert finish.status_code == 200
    assert finish.json()["reason"] == "finished"
    while not q.empty():
        events.append(q.get_nowait())
    kinds = [getattr(ev, "event", None) for ev in events]
    assert "final" in kinds
    assert "closed" in kinds
    final = next(ev for ev in events if ev.event == "final")
    assert final.text == "fixture-final"
    encoded = format_sse(SessionFinalEvent(session_id=sid, text="fixture-final", language="zh"))
    assert encoded.startswith("event: final")
    assert "fixture-final" in encoded
    encoded_closed = format_sse(SessionClosedEvent(session_id=sid, reason="finished"))
    assert "finished" in encoded_closed
    client.app.state.sessions.unsubscribe(sid, q)
    # 重复 finish 409
    again = client.post(f"/api/v1/recognize/sessions/{sid}/finish", headers=local_headers())
    assert again.status_code == 409

    other = client.post("/api/v1/recognize/sessions", json={"language": "zh"}, headers=local_headers()).json()["session_id"]
    cancelled = client.post(f"/api/v1/recognize/sessions/{other}/cancel", headers=local_headers())
    assert cancelled.status_code == 200
    assert cancelled.json()["reason"] == SessionClosedReason.cancelled
    utterance = client.post(
        "/api/v1/utterances",
        json={"job_id": "job-cancel", "session_id": other, "source": "desktop", "focus_id": "f"},
        headers=local_headers(),
    )
    assert utterance.status_code == 400
    assert runtime.jobs.get("job-cancel").found is False


def test_disconnect_before_finish_is_visible_and_does_not_queue(app, runtime):
    client = local_client(app)
    sid = client.post("/api/v1/recognize/sessions", json={"language": "zh"}, headers=local_headers()).json()["session_id"]
    client.app.state.sessions.mark_disconnected(sid)
    snap = runtime.finals.lookup(sid)
    assert snap is not None
    assert snap.closed_reason == SessionClosedReason.disconnected
    finish = client.post(f"/api/v1/recognize/sessions/{sid}/finish", headers=local_headers())
    assert finish.status_code == 409
    utterance = client.post(
        "/api/v1/utterances",
        json={"job_id": "job-disc", "session_id": sid, "source": "desktop", "focus_id": "f"},
        headers=local_headers(),
    )
    assert utterance.status_code == 400
    assert runtime.jobs.get("job-disc").found is False


def test_missing_stream_provider_is_unavailable_not_fake_success(tmp_path: Path):
    runtime = build_sandbox_runtime(
        root=tmp_path / "no-stream",
        token=TEST_TOKEN,
        transcribe=fixture_transcribe,
        stream_provider=None,
    )
    app = create_app(runtime)
    client = local_client(app)
    res = client.post("/api/v1/recognize/sessions", json={"language": "zh"}, headers=local_headers())
    assert res.status_code == 503
    body = res.json()
    assert body["error"] == ErrorCode.asr_unavailable
    assert body["ok"] is False
    assert "fixture" not in body.get("session_id", "")


def test_unauthorized_sse_is_403(app):
    remote = lan_client(app)
    res = remote.get("/api/v1/recognize/sessions/x/events", headers=lan_headers())
    assert res.status_code == 403
    product = remote.get("/api/v1/events", headers=lan_headers())
    assert product.status_code == 403


def test_fixture_provider_is_not_official_engine():
    provider = FixtureAsrProvider()
    assert provider.official_engine is False
