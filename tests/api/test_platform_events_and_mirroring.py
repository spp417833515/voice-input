"""seg2 新增 API 行为：平台状态入口、识别会话→产品事件镜像、ack none → delivery_failed、
claim 长轮询、resend 覆盖焦点。全部 sandbox runtime + 假提供者。"""
from __future__ import annotations

import threading
import time

from _contracts import ErrorCode, SessionErrorEvent, SessionPartialEvent
from tests.api.conftest import lan_client, lan_headers, local_client, local_headers


def _capture(runtime) -> list[dict]:
    seen: list[dict] = []
    runtime.events.subscribe(lambda e: seen.append(e.model_dump(mode="json")))
    return seen


def test_platform_event_accepts_idle_busy_error_only(app, runtime):
    seen = _capture(runtime)
    c = local_client(app)
    for body in ({"event": "busy"}, {"event": "idle"}, {"event": "error", "error": "dependency_failed", "message": "麦克风打开失败"}):
        res = c.post("/api/v1/events/platform", json=body, headers=local_headers())
        assert res.status_code == 202, res.text
        assert res.json() == {"ok": True}
    assert [e["event"] for e in seen] == ["busy", "idle", "error"]
    for forged in ({"event": "typed", "job_id": "x", "text": "t"}, {"event": "recording"}, {"event": "error", "error": "nope", "message": "m"}, {"event": "busy", "extra": 1}):
        assert c.post("/api/v1/events/platform", json=forged, headers=local_headers()).status_code == 400, forged
    assert len(seen) == 3


def test_platform_event_is_local_only(app):
    res = lan_client(app).post("/api/v1/events/platform", json={"event": "busy"}, headers=lan_headers())
    assert res.status_code in (401, 403)


def test_session_mirrors_recording_partial_and_cancel_idle(app, runtime):
    seen = _capture(runtime)
    c = local_client(app)
    sid = c.post("/api/v1/recognize/sessions", json={}, headers=local_headers()).json()["session_id"]
    hub = app.state.sessions
    hub.publish_partial(sid, SessionPartialEvent(session_id=sid, stable_text="你", tail_text="好"))
    assert c.post(f"/api/v1/recognize/sessions/{sid}/cancel", headers=local_headers()).status_code == 200
    kinds = [e["event"] for e in seen]
    assert kinds == ["recording", "partial", "idle"], kinds
    assert seen[0]["session_id"] == sid
    assert seen[1]["stable_text"] == "你" and seen[1]["tail_text"] == "好"


def test_session_provider_error_mirrors_error(app, runtime):
    seen = _capture(runtime)
    c = local_client(app)
    sid = c.post("/api/v1/recognize/sessions", json={}, headers=local_headers()).json()["session_id"]
    app.state.sessions.publish_provider_error(
        sid, SessionErrorEvent(session_id=sid, error=ErrorCode.asr_unavailable, message="sidecar 断开")
    )
    errs = [e for e in seen if e["event"] == "error"]
    assert errs and errs[0]["error"] == "asr_unavailable" and errs[0]["message"] == "sidecar 断开"
    assert [e["event"] for e in seen].count("idle") == 0  # error 关闭不额外发 idle


def _queue_job(c, job_id: str, focus: str = "7") -> None:
    sid = c.post("/api/v1/recognize/sessions", json={}, headers=local_headers()).json()["session_id"]
    c.post(f"/api/v1/recognize/sessions/{sid}/finish", headers=local_headers())
    res = c.post(
        "/api/v1/utterances",
        json={"job_id": job_id, "session_id": sid, "source": "desktop", "focus_id": focus},
        headers=local_headers(),
    )
    assert res.status_code == 200, res.text


def test_ack_none_publishes_delivery_failed(app, runtime):
    c = local_client(app)
    _queue_job(c, "job-none")
    seen = _capture(runtime)
    claimed = c.post("/api/v1/jobs/claim", json={"timeout_s": 0}, headers=local_headers()).json()
    res = c.post(
        f"/api/v1/jobs/{claimed['job_id']}/ack",
        json={"claim_id": claimed["claim_id"], "result": "none", "detail": "焦点与任务绑定时不一致，未注入"},
        headers=local_headers(),
    )
    assert res.status_code == 200 and res.json()["state"] == "failed"
    err = [e for e in seen if e["event"] == "error"]
    assert err and err[0]["error"] == "delivery_failed"
    assert err[0]["message"] == "焦点与任务绑定时不一致，未注入"
    assert err[0]["details"] == {"job_id": "job-none"}


def test_claim_long_poll_returns_job_enqueued_while_waiting(app, runtime):
    c = local_client(app)
    result: dict = {}

    def claim():
        t0 = time.monotonic()
        result["body"] = c.post("/api/v1/jobs/claim", json={"timeout_s": 5}, headers=local_headers()).json()
        result["elapsed"] = time.monotonic() - t0

    t = threading.Thread(target=claim)
    t.start()
    time.sleep(0.4)
    _queue_job(local_client(app), "job-late")
    t.join(10)
    assert result["body"]["job_id"] == "job-late"
    assert 0.3 < result["elapsed"] < 4.0


def test_claim_without_wait_returns_null_immediately(app):
    t0 = time.monotonic()
    res = local_client(app).post("/api/v1/jobs/claim", json={"timeout_s": 0}, headers=local_headers())
    assert res.status_code == 200 and res.json() is None
    assert time.monotonic() - t0 < 1.0


def test_resend_focus_override_and_inherit(app):
    c = local_client(app)
    _queue_job(c, "job-orig", focus="7")
    claimed = c.post("/api/v1/jobs/claim", json={"timeout_s": 0}, headers=local_headers()).json()
    c.post(f"/api/v1/jobs/{claimed['job_id']}/ack", json={"claim_id": claimed["claim_id"], "result": "complete"}, headers=local_headers())
    over = c.post(
        "/api/v1/utterances/resend",
        json={"job_id": "job-r1", "text": "再来", "original_job_id": "job-orig", "focus_id": "8", "confirm": True},
        headers=local_headers(),
    )
    assert over.status_code == 200, over.text
    assert c.get("/api/v1/jobs/job-r1", headers=local_headers()).json()["job"]["focus_id"] == "8"
    inherit = c.post(
        "/api/v1/utterances/resend",
        json={"job_id": "job-r2", "text": "再来", "original_job_id": "job-orig"},
        headers=local_headers(),
    )
    assert inherit.status_code == 200, inherit.text
    assert c.get("/api/v1/jobs/job-r2", headers=local_headers()).json()["job"]["focus_id"] == "7"
