"""定稿失败（如引擎拒绝复读失控的定稿）时，sidecar 关闭会话并释放调度槽，不挂到空闲超时。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from _contracts import ErrorCode, LogicError
from runtimes.asr.server import create_sidecar_app
from tests.sidecar.fake_engine import FakeEngine


class _FailingFinish(FakeEngine):
    def finish_stream(self, session_id: str):
        self.streams.pop(session_id, None)
        raise LogicError(ErrorCode.asr_unavailable, "定稿解码超过语速上界")


def test_finish_failure_closes_session_and_releases_engine():
    engine = _FailingFinish()
    app = create_sidecar_app(engine)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={}).json()["session_id"]
    client.post(f"/v1/sessions/{sid}/chunks", content=b"\x00\x00" * 160, headers={"X-Chunk-Index": "0"})
    resp = client.post(f"/v1/sessions/{sid}/finish")
    assert resp.status_code == 503
    assert resp.json()["error"] == ErrorCode.asr_unavailable.value
    assert sid not in app.state.sessions
    assert app.state.scheduler.active_stream_id is None
    # 槽位已释放：可以立刻开新会话
    assert client.post("/v1/sessions", json={}).status_code == 200
    assert client.post(f"/v1/sessions/{sid}/cancel").status_code == 404
