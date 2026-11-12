from __future__ import annotations

from tests.api.conftest import (
    LAN_HOST,
    TEST_TOKEN,
    lan_client,
    lan_headers,
    local_client,
    local_headers,
)


def test_local_status_ok_and_does_not_echo_token(app):
    client = local_client(app)
    res = client.get("/api/v1/status")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["unmet_gates"]
    dumped = str(body)
    assert TEST_TOKEN not in dumped
    assert "token" not in dumped.lower() or "unmet" in dumped.lower()


def test_remote_status_and_claim_forbidden_even_with_token(app):
    client = lan_client(app)
    headers = lan_headers()
    status = client.get("/api/v1/status", headers=headers)
    assert status.status_code == 403
    assert status.json()["error"] == "forbidden"
    claim = client.post("/api/v1/jobs/claim", json={}, headers=headers)
    assert claim.status_code == 403
    stream = client.get("/api/v1/recognize/sessions/does-not-matter/events", headers=headers)
    assert stream.status_code == 403
    events = client.get("/api/v1/events", headers=headers)
    assert events.status_code == 403
    ack = client.post("/api/v1/jobs/job-x/ack", json={"claim_id": "c", "result": "complete"}, headers=headers)
    assert ack.status_code == 403
    cfg = client.post("/api/v1/config", json={"polish_enabled": True}, headers=headers)
    assert cfg.status_code == 403
    setup = client.post("/api/setup/pull_model", headers=headers)
    assert setup.status_code == 403


def test_x_forwarded_for_cannot_spoof_localhost(app):
    client = lan_client(app)
    spoof = lan_headers()
    spoof["X-Forwarded-For"] = "127.0.0.1"
    spoof["Forwarded"] = "for=127.0.0.1"
    res = client.get("/api/v1/status", headers=spoof)
    assert res.status_code == 403


def test_lan_stt_requires_valid_token(app):
    client = lan_client(app)
    missing = client.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("a.txt", b"hello", "application/octet-stream")},
        headers={"Origin": f"https://{LAN_HOST}:17945"},
    )
    assert missing.status_code == 403
    wrong = client.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("a.txt", b"hello", "application/octet-stream")},
        headers=lan_headers(token="wrong-token-not-the-test-one"),
    )
    assert wrong.status_code == 403
    ok = client.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("a.txt", b"hello", "application/octet-stream")},
        headers=lan_headers(),
    )
    assert ok.status_code == 200
    assert ok.json()["text"] == "hello"


def test_unknown_origin_rejected_on_write(app):
    client = local_client(app)
    res = client.post(
        "/api/v1/config",
        json={"polish_enabled": False},
        headers={"Origin": "https://evil.example:17945"},
    )
    assert res.status_code == 403
    assert "CSRF" in res.json()["message"]
    lan = lan_client(app)
    bad = lan.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("a.txt", b"hello", "application/octet-stream")},
        headers={
            "Origin": "https://evil.example:17945",
            "Host": f"{LAN_HOST}:17945",
            "X-Voice-Token": TEST_TOKEN,
        },
    )
    assert bad.status_code == 403


def test_local_same_origin_write_allowed(app):
    client = local_client(app)
    res = client.post(
        "/api/v1/config",
        json={"polish_enabled": False},
        headers=local_headers(),
    )
    assert res.status_code == 200
    assert res.json()["polish_enabled"] is False


def test_no_cors_star_header(app):
    client = local_client(app)
    res = client.get("/api/v1/status", headers={"Origin": "https://127.0.0.1:17945"})
    acao = res.headers.get("access-control-allow-origin")
    assert acao not in {"*", "https://*"}
    assert acao is None
