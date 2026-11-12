from __future__ import annotations

import json

import httpx
import pytest

from _jobs_client import JobsClient


class _FakeTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.acks: list[dict] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/api/v1/jobs/claim"):
            return httpx.Response(200, json={"job_id": "j1", "claim_id": "c1", "text": "hi"})
        if request.method == "GET" and "/api/v1/jobs/j1" in url:
            return httpx.Response(200, json={"found": True, "state": "claimed", "job_id": "j1"})
        if request.method == "POST" and url.endswith("/ack"):
            payload = json.loads(request.content.decode())
            self.acks.append(payload)
            return httpx.Response(200, json={"ok": True, "state": payload["result"]})
        return httpx.Response(404, json={"error": "not_found"})


def test_jobs_client_ack_idempotent_and_loopback_only():
    transport = _FakeTransport()
    with JobsClient(base_url="http://127.0.0.1:17945", transport=transport) as client:
        claimed = client.claim()
        assert claimed["claim_id"] == "c1"
        body = client.ack("j1", "c1", "complete")
        assert body["state"] == "complete"
        again = client.ack("j1", "c1", "complete")
        assert again["state"] == "complete"
        with pytest.raises(ValueError):
            client.ack("j1", "c1", "none")
    with pytest.raises(ValueError):
        JobsClient(base_url="https://example.com:17945")
