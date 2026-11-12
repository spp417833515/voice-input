"""B2：HTTP FakeEngine SSE 一订阅就 early partial；失败 start / 无 open 僵尸 / 不从 history 重建。

保留 tests/integration/test_sidecar_provider_partials.py 中的 B1 回归，本文件只覆盖启动次序。
"""
from __future__ import annotations

import threading
from pathlib import Path

from _contracts import ErrorCode, LogicError
from _v3.sessions import session_replay_source
from _v3.isolated import compose_isolated_candidate
from tests.api.test_session_start_lifecycle import ScriptedProvider
from tests.integration.fixtures import fixture_transcribe
from tests.integration.test_sidecar_provider_partials import (
    _app_headers,
    _create_session,
    _leftover_reader_threads,
    _post_finish,
    _submit,
    fake_sidecar,
    isolated_sidecar_app,
    start_app_sse,
    stop_app_sse,
)
from tests.sidecar.fake_engine import FakeEngine


def test_http_fake_engine_early_partial_on_sse_subscribe_reaches_app(tmp_path: Path):
    engine = FakeEngine(text="sidecar-fixture-final", early_partials=["early-http-tail"])
    with fake_sidecar(engine=engine) as (_eng, _server, _client, provider):
        assert type(provider).__name__ == "SidecarStreamProvider"
        with isolated_sidecar_app(tmp_path, provider) as comp:
            headers = _app_headers(comp)
            sid = _create_session(comp, headers)
            session = comp.app.state.sessions._sessions[sid]
            assert session.state == "open"
            assert session.provider_ready is True
            pump = start_app_sse(
                comp.app,
                path=f"/api/v1/recognize/sessions/{sid}/events",
                headers=headers,
                bind=(comp.bind_host, comp.bind_port),
            )
            try:
                # 不发 chunk：只依赖 sidecar SSE 一订阅就发出的 early partial。
                # 读线程可能略晚于 create 返回，但会话已登记，事件不得丢失。
                tails = pump.wait_partials(1)
                assert tails == ["early-http-tail"]
                assert "final" not in pump.kinds()
                assert session.latest_partial is not None
                assert session.latest_partial.tail_text == "early-http-tail"
                finish = _post_finish(comp, headers, sid)
                assert finish["status"] == 200, finish
                pump.wait_closed()
                kinds = pump.kinds()
                assert kinds[0] == "partial"
                assert kinds.count("final") == 1
                assert kinds[-1] == "closed"
                assert kinds.index("partial") < kinds.index("final")
                submitted = _submit(comp, headers, sid, "job-early-b2")
                assert submitted["status"] == 200, submitted
                assert submitted["body"]["text"] == "sidecar-fixture-final"
            finally:
                stop_app_sse(pump)
            history = comp.test_client().get("/api/v1/history", headers=headers)
            assert history.status_code == 200
            # 成功路径可以有历史；本断言只确认不是从 history 把会话重建出来
            assert sid in comp.app.state.sessions._sessions
        assert not _leftover_reader_threads()


def test_failed_start_http_is_not_200_and_leaves_no_open_zombie(tmp_path: Path):
    provider = ScriptedProvider(raise_after=LogicError(ErrorCode.asr_unavailable, "sidecar 启动失败"))
    comp = compose_isolated_candidate(
        root=tmp_path / "start-fail-iso",
        token="test-isolated-start-fail-token",
        transcribe=fixture_transcribe,
        stream_provider=provider,
        fixture_notes=("ScriptedProvider 启动失败，非官方引擎",),
        mount_static=False,
    )
    try:
        client = comp.test_client()
        headers = comp.local_headers()
        created = client.post("/api/v1/recognize/sessions", json={"language": "zh"}, headers=headers)
        assert created.status_code != 200, created.text
        assert created.status_code >= 400
        assert provider.started
        sid = next(iter(provider.started))
        session = comp.app.state.sessions._sessions[sid]
        assert session.state != "open"
        assert session.state == "error"
        assert session.provider_ready is False
        assert session.final is None
        assert "final" not in [ev.event for ev in session_replay_source(session)]
        finish = client.post(f"/api/v1/recognize/sessions/{sid}/finish", headers=headers)
        assert finish.status_code == 409, finish.text
        submitted = client.post(
            "/api/v1/utterances",
            json={"job_id": "job-start-fail", "session_id": sid, "source": "desktop", "focus_id": "f"},
            headers=headers,
        )
        assert submitted.status_code in {400, 404, 409, 422}, submitted.text
        history = client.get("/api/v1/history", headers=headers).json()
        assert history["total"] == 0
        jobs = list(comp.runtime.jobs._store.list_jobs())
        assert jobs == []
        snap = comp.runtime.finals.lookup(sid)
        assert snap is not None
        assert snap.has_final is False
        assert sid in provider.cancelled
        assert provider.finished == []
    finally:
        comp.close()
    assert not _leftover_reader_threads()


def test_http_create_survives_b1_happy_path_without_early_hook(tmp_path: Path):
    """默认 FakeEngine 无 early hook 时，原 B1 顺序仍成立：chunk 后、finish 前有字幕。"""
    from tests.integration.test_sidecar_provider_partials import _post_chunk

    with fake_sidecar(engine=FakeEngine(text="sidecar-fixture-final")) as (_e, _s, _c, provider):
        with isolated_sidecar_app(tmp_path, provider) as comp:
            headers = _app_headers(comp)
            sid = _create_session(comp, headers)
            pump = start_app_sse(
                comp.app,
                path=f"/api/v1/recognize/sessions/{sid}/events",
                headers=headers,
                bind=(comp.bind_host, comp.bind_port),
            )
            try:
                chunk = _post_chunk(comp, headers, sid)
                assert chunk["status"] == 202, chunk
                pump.wait_partials(1)
                assert "final" not in pump.kinds()
                finish = _post_finish(comp, headers, sid)
                assert finish["status"] == 200, finish
                pump.wait_closed()
                assert pump.kinds() == ["partial", "final", "closed"]
            finally:
                stop_app_sse(pump)
        assert not _leftover_reader_threads()
