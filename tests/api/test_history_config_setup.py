from __future__ import annotations

from tests.api.conftest import TEST_TOKEN, lan_client, lan_headers, local_client, local_headers


def test_history_and_status_and_setup_not_faked(app, runtime):
    client = local_client(app)
    # 先造一条处理记录
    created = client.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "job-hist", "focus_id": "f"},
        files={"audio": ("a.txt", b"keep", "text/plain")},
        headers=local_headers(),
    )
    assert created.status_code == 200
    history = client.get("/api/v1/history?limit=10").json()
    assert history["total"] == 1
    job = client.get("/api/v1/jobs/job-hist").json()
    assert job["found"] is True
    assert job["job"]["state"] == "queued"
    assert history["records"][0].get("state") in (None, "queued") or "state" not in history["records"][0]

    status = client.get("/api/v1/status").json()
    assert status["unmet_gates"]
    assert "qwen3.5:4b" in " ".join(status["unmet_gates"]) and "真机" in " ".join(status["unmet_gates"])
    assert TEST_TOKEN not in str(status)

    # 未注入设置探测时不得返回假就绪；完整设置用例见 test_setup_lan_config_pages.py
    check = client.get("/api/setup/check")
    assert check.status_code == 503
    assert check.json()["ok"] is False


def test_lan_history_allowed_config_forbidden(app):
    local = local_client(app)
    local.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "job-read", "focus_id": "f"},
        files={"audio": ("a.txt", b"keep", "text/plain")},
        headers=local_headers(),
    )
    remote = lan_client(app)
    hist = remote.get("/api/v1/history", headers=lan_headers())
    assert hist.status_code == 200
    assert hist.json()["total"] >= 1
    cfg = remote.get("/api/v1/config", headers=lan_headers())
    assert cfg.status_code == 403


def test_product_events_sse_typed_models(app, runtime):
    from _contracts import ProductBusyEvent, ProductIdleEvent
    from _v3.http import format_sse

    seen: list[str] = []
    runtime.events.subscribe(lambda ev: seen.append(ev.event.value if hasattr(ev.event, "value") else ev.event))
    runtime.events.publish(ProductBusyEvent())
    assert seen == ["busy"]
    idle = format_sse(ProductIdleEvent())
    busy = format_sse(ProductBusyEvent())
    assert idle.startswith("event: idle")
    assert busy.startswith("event: busy")
    # 长连接体由 TestClient 读取会阻塞；鉴权已覆盖远程 403。此处只确认本地入口存在。
    assert any(getattr(route, "path", None) == "/api/v1/events" for route in app.routes)
