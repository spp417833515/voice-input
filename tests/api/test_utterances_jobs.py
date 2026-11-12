from __future__ import annotations

from _contracts import AckResult, JobState, SIDECAR_CHUNK_HEADER
from tests.api.conftest import lan_client, lan_headers, local_client, local_headers


def _pcm(samples: int = 16) -> bytes:
    return b"\x01\x00" * samples


def _finish_session(client, *, language="zh") -> str:
    created = client.post("/api/v1/recognize/sessions", json={"language": language}, headers=local_headers())
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]
    chunk = client.post(
        f"/api/v1/recognize/sessions/{session_id}/chunks",
        content=_pcm(),
        headers={**local_headers(), SIDECAR_CHUNK_HEADER: "0", "Content-Type": "application/octet-stream"},
    )
    assert chunk.status_code == 202, chunk.text
    finished = client.post(f"/api/v1/recognize/sessions/{session_id}/finish", headers=local_headers())
    assert finished.status_code == 200, finished.text
    assert finished.json()["reason"] == "finished"
    return session_id


def test_desktop_utterance_claim_ack_and_history_is_not_delivery(app, runtime):
    client = local_client(app)
    session_id = _finish_session(client)
    res = client.post(
        "/api/v1/utterances",
        json={"job_id": "job-desk", "session_id": session_id, "source": "desktop", "focus_id": "focus-A"},
        headers=local_headers(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["state"] == "queued"
    assert body["job_id"] == "job-desk"
    assert body["text"] == "fixture-final"
    queried = client.get("/api/v1/jobs/job-desk", headers=local_headers())
    assert queried.status_code == 200
    assert queried.json()["found"] is True
    assert queried.json()["job"]["focus_id"] == "focus-A"
    assert queried.json()["job"]["state"] == "queued"
    claimed = client.post("/api/v1/jobs/claim", json={}, headers=local_headers())
    assert claimed.status_code == 200
    assert claimed.json()["job_id"] == "job-desk"
    assert claimed.json()["focus_id"] == "focus-A"
    ack = client.post(
        "/api/v1/jobs/job-desk/ack",
        json={"claim_id": claimed.json()["claim_id"], "result": "complete"},
        headers=local_headers(),
    )
    assert ack.status_code == 200
    assert ack.json()["state"] == "done"
    done = client.get("/api/v1/jobs/job-desk").json()
    assert done["job"]["state"] == JobState.done
    history = client.get("/api/v1/history").json()
    assert history["total"] == 1
    record = history["records"][0]
    assert record["job_id"] == "job-desk"
    assert record["final_text"] == "fixture-final"
    assert "state" not in record or record.get("state") != "done"
    # 投递状态不能靠历史冒充
    assert "done" not in record


def test_from_audio_requires_client_job_id_and_keeps_focus(app):
    client = local_client(app)
    missing = client.post(
        "/api/v1/utterances/from-audio",
        data={"language": "zh", "focus_id": "focus-B"},
        files={"audio": ("a.txt", b"phone-text", "text/plain")},
        headers=local_headers(),
    )
    assert missing.status_code == 400
    assert "job_id" in missing.json()["message"]
    created = client.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "job-phone", "language": "zh", "focus_id": "focus-B"},
        files={"audio": ("a.txt", b"phone-text", "text/plain")},
        headers=local_headers(),
    )
    assert created.status_code == 200, created.text
    job = client.get("/api/v1/jobs/job-phone").json()["job"]
    assert job["focus_id"] == "focus-B"
    assert job["text"] == "phone-text"
    # 同 id 不同 payload 409
    conflict = client.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "job-phone", "language": "zh", "focus_id": "focus-C"},
        files={"audio": ("a.txt", b"other-text", "text/plain")},
        headers=local_headers(),
    )
    assert conflict.status_code == 409
    # 幂等重试
    again = client.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "job-phone", "language": "zh", "focus_id": "focus-B"},
        files={"audio": ("a.txt", b"phone-text", "text/plain")},
        headers=local_headers(),
    )
    assert again.status_code == 200
    assert again.json()["job_id"] == "job-phone"


def test_resend_confirmation_and_late_ack(app):
    client = local_client(app)
    first = client.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "job-old", "focus_id": "focus-R"},
        files={"audio": ("a.txt", b"once", "text/plain")},
        headers=local_headers(),
    )
    assert first.status_code == 200
    claimed = client.post("/api/v1/jobs/claim", json={}, headers=local_headers())
    claim_id = claimed.json()["claim_id"]
    unknown = client.post(
        "/api/v1/jobs/job-old/ack",
        json={"claim_id": claim_id, "result": "partial_or_unknown"},
        headers=local_headers(),
    )
    assert unknown.json()["state"] == "delivery_unknown"
    pending = client.post(
        "/api/v1/utterances/resend",
        json={"job_id": "job-new", "text": "once", "original_job_id": "job-old"},
        headers=local_headers(),
    )
    assert pending.status_code == 200
    body = pending.json()
    assert body["ok"] is False
    assert body["requires_confirmation"] is True
    assert client.get("/api/v1/jobs/job-new").json()["found"] is False
    confirmed = client.post(
        "/api/v1/utterances/resend",
        json={"job_id": "job-new", "text": "once", "original_job_id": "job-old", "confirm": True},
        headers=local_headers(),
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["ok"] is True
    # 晚到且匹配的 ack 收敛，不重投
    late = client.post(
        "/api/v1/jobs/job-old/ack",
        json={"claim_id": claim_id, "result": "complete"},
        headers=local_headers(),
    )
    assert late.status_code == 200
    assert late.json()["state"] == "done"
    assert client.get("/api/v1/jobs/job-old").json()["job"]["state"] == "done"


def test_unknown_job_query_is_unknown_not_failed(app):
    client = local_client(app)
    res = client.get("/api/v1/jobs/missing-id")
    assert res.status_code == 200
    body = res.json()
    assert body["found"] is False
    assert body["state"] == "unknown"


def test_lan_can_query_job_but_cannot_claim(app):
    client = local_client(app)
    created = client.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "job-lanq", "focus_id": "f"},
        files={"audio": ("a.txt", b"text", "text/plain")},
        headers=local_headers(),
    )
    assert created.status_code == 200
    remote = lan_client(app)
    query = remote.get("/api/v1/jobs/job-lanq", headers=lan_headers())
    assert query.status_code == 200
    assert query.json()["job"]["state"] == "queued"
    claim = remote.post("/api/v1/jobs/claim", json={}, headers=lan_headers())
    assert claim.status_code == 403
