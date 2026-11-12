"""B2 / Host / 路径规范化：局域网允许名单与 Host 校验。"""
from __future__ import annotations

import asyncio
import ipaddress

from fastapi.testclient import TestClient

from _v3.auth import access_mode, is_allowed_lan_ip, parse_ip
from tests.api.asgi_client import call_asgi, multipart
from tests.api.conftest import (
    LAN_HOST,
    TEST_TOKEN,
    lan_client,
    lan_headers,
    local_client,
    local_headers,
)


def test_lan_allowlist_rejects_public_reserved_and_docs():
    allowed = ["10.0.0.8", "172.16.5.5", "192.168.10.20", "169.254.1.1", "fc00::1", "fe80::1", "::ffff:192.168.0.7"]
    rejected = [
        "8.8.8.8",
        "198.18.75.174",
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.1",
        "100.64.0.1",
        "2001:db8::1",
        "1.1.1.1",
        "::ffff:8.8.8.8",
        "::ffff:198.18.0.1",
        "127.0.0.1",
        "::1",
    ]
    for item in allowed:
        ip = parse_ip(item)
        assert ip is not None and is_allowed_lan_ip(ip), item
    for item in rejected:
        ip = parse_ip(item)
        assert ip is None or not is_allowed_lan_ip(ip), item
    assert parse_ip("not-an-ip") is None
    assert parse_ip("") is None
    mapped = parse_ip("::ffff:10.1.2.3")
    assert isinstance(mapped, ipaddress.IPv4Address) and is_allowed_lan_ip(mapped)


def test_claim_trailing_slash_is_local_without_redirect(app):
    assert access_mode("POST", "/api/v1/jobs/claim/") == "local"
    assert access_mode("POST", "/api/v1/jobs/claim") == "local"
    lan = lan_client(app)
    denied = lan.post("/api/v1/jobs/claim/", headers=lan_headers(), follow_redirects=False)
    assert denied.status_code == 403, denied.text
    assert "仅本机" in denied.json()["message"]
    local = local_client(app)
    ok = local.post("/api/v1/jobs/claim/", headers=local_headers(), follow_redirects=False)
    assert ok.status_code == 200, ok.text
    assert ok.json() is None


def test_default_testserver_host_is_rejected(app):
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.get("/api/v1/status")
    assert response.status_code == 403
    assert "Host" in response.json()["message"]


def test_local_origin_cannot_ride_private_host(app):
    client = local_client(app)
    response = client.post(
        "/api/v1/config",
        headers={"Origin": "https://127.0.0.1:17945", "Host": "192.168.10.20:17945"},
        json={"polish_enabled": True},
    )
    assert response.status_code == 403
    assert client.get("/api/v1/config", headers=local_headers()).json()["polish_enabled"] is False


def test_cli_without_origin_still_requires_trusted_host(app):
    client = local_client(app)
    denied = client.post("/api/v1/config", headers={"Host": "evil.example:17945"}, json={"polish_enabled": True})
    assert denied.status_code == 403
    allowed = client.post("/api/v1/config", headers={"Host": "127.0.0.1:17945"}, json={"polish_enabled": True})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["polish_enabled"] is True


def test_lan_and_public_token_matrix(app):
    body, ctype = multipart([("audio", b"from-lan", "a.txt")])

    def _host_header(host: str) -> str:
        if ":" in host and not host.startswith("["):
            return f"[{host}]:17945"
        return f"{host}:17945"

    def stt(client_addr: tuple[str, int], host: str, token: str | None = TEST_TOKEN, origin: str | None = None):
        headers = {"host": _host_header(host), "content-type": ctype}
        if token:
            headers["authorization"] = f"Bearer {token}"
        if origin:
            headers["origin"] = origin
        return asyncio.run(
            call_asgi(
                app,
                method="POST",
                path="/api/v1/stt",
                client=client_addr,
                headers=headers,
                body=body,
            )
        )

    lan = stt(("192.168.10.20", 50000), "192.168.10.20", origin="https://192.168.10.20:17945")
    assert lan["status"] == 200, lan
    ula = stt(("fd12:3456::10", 50000), "fd12:3456::10", origin="https://[fd12:3456::10]:17945")
    assert ula["status"] == 200, ula
    public = stt(("8.8.8.8", 50000), "8.8.8.8")
    assert public["status"] == 403, public
    public_lan_host = stt(("8.8.8.8", 50000), "192.168.10.20", origin="https://192.168.10.20:17945")
    assert public_lan_host["status"] == 403, public_lan_host
    assert "局域网" in public_lan_host["body"]["message"]
    fake = stt(("198.18.75.174", 50000), "198.18.75.174")
    assert fake["status"] == 403, fake
    docs = stt(("192.0.2.1", 50000), "192.0.2.1")
    assert docs["status"] == 403, docs
    invalid = stt(("not-an-ip", 50000), "192.168.10.20")
    assert invalid["status"] == 403, invalid
    mapped_pub = stt(("::ffff:8.8.8.8", 50000), "8.8.8.8")
    assert mapped_pub["status"] == 403, mapped_pub
    mapped_lan = stt(("::ffff:10.0.0.8", 50000), "10.0.0.8", origin="https://10.0.0.8:17945")
    assert mapped_lan["status"] == 200, mapped_lan


def test_xff_cannot_make_public_peer_local_or_lan(app):
    body, ctype = multipart([("audio", b"xff", "a.txt")])
    result = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path="/api/v1/stt",
            client=("8.8.8.8", 50000),
            headers={
                "host": "127.0.0.1:17945",
                "authorization": f"Bearer {TEST_TOKEN}",
                "content-type": ctype,
                "x-forwarded-for": "127.0.0.1",
            },
            body=body,
        )
    )
    assert result["status"] == 403, result


def test_loopback_ipv6_can_read_status(app):
    result = asyncio.run(
        call_asgi(
            app,
            method="GET",
            path="/api/v1/status",
            client=("::1", 50000),
            headers={"host": "[::1]:17945"},
        )
    )
    assert result["status"] == 200, result
    assert result["body"]["ok"] is True
    assert result["body"]["unmet_gates"]
