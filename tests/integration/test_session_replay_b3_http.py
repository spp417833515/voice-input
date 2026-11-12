"""B3：随机回环 HTTP FakeEngine 上应用 session SSE 的完整生命周期与有限回放。"""
from __future__ import annotations

from pathlib import Path

from _v3.sessions import session_replay_source
from tests.integration.test_sidecar_provider_partials import (
    _app_headers,
    _create_session,
    _leftover_reader_threads,
    _post_chunk,
    _post_finish,
    _submit,
    fake_sidecar,
    isolated_sidecar_app,
    start_app_sse,
    stop_app_sse,
)
from tests.sidecar.fake_engine import FakeEngine


def test_http_fake_engine_app_sse_full_lifecycle_and_bounded_replay(tmp_path: Path):
    engine = FakeEngine(
        text="sidecar-fixture-final",
        early_partials=["early-http-tail"],
        partials_per_chunk=1,
    )
    with fake_sidecar(engine=engine) as (_eng, server, _client, provider):
        assert type(provider).__name__ == "SidecarStreamProvider"
        assert server.port not in {17945, 17946, 11434}
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
                pump.wait_partials(1)
                assert pump.tails()[0] == "early-http-tail"
                for index in range(3):
                    chunk = _post_chunk(comp, headers, sid, index=index)
                    assert chunk["status"] == 202, chunk
                # FakeEngine 每个 chunk 的 tail 都是同一终稿文本；至少再等到字幕仍在 finish 前。
                pump.wait_partials(1)
                assert "final" not in pump.kinds()
                finish = _post_finish(comp, headers, sid)
                assert finish["status"] == 200, finish
                pump.wait_closed()
                kinds = pump.kinds()
                assert kinds[0] == "partial"
                assert kinds.count("final") == 1
                assert kinds[-2:] == ["final", "closed"]
                session = comp.app.state.sessions._sessions[sid]
                source = session_replay_source(session)
                assert [ev.event for ev in source] == ["partial", "final", "closed"]
                assert len(source) <= 3
                late = comp.app.state.sessions.subscribe(sid)
                late_events = []
                while not late.empty():
                    late_events.append(late.get_nowait())
                late_kinds = [ev.event for ev in late_events]
                assert late_kinds[-2:] == ["final", "closed"]
                assert late_kinds.count("final") == 1
                assert late.overflow is False
                comp.app.state.sessions.unsubscribe(sid, late)
                submitted = _submit(comp, headers, sid, "job-replay-b3")
                assert submitted["status"] == 200, submitted
                assert submitted["body"]["text"] == "sidecar-fixture-final"
            finally:
                stop_app_sse(pump)
        assert not _leftover_reader_threads()
