"""隔离候选组合：复用 create_app(runtime) 走会话→入队→claim/ack。不是真实端到端验收。"""
from __future__ import annotations

from pathlib import Path

from _contracts import SIDECAR_CHUNK_HEADER
from _inject import FocusBoundInjector, RecordingBackend
from _jobs_client import JobsClient
from _v3.isolated import FORBIDDEN_PORTS, compose_isolated_candidate, compose_production_runtime, production_unmet_gates
from _v3.runtime import UnmetRuntimeGates
from tests.integration.fixtures import FixtureStreamProvider, fixture_transcribe

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _pcm(n: int = 16) -> bytes:
    return b"\x01\x00" * n


def test_production_compose_still_blocked():
    try:
        compose_production_runtime()  # type: ignore[call-arg]
    except TypeError:
        pass
    else:
        raise AssertionError("compose_production_runtime 必须显式给出路径、令牌与系统适配器")
    assert production_unmet_gates(), "官方/真机门闩未关闭前不得清空"
    assert issubclass(UnmetRuntimeGates, RuntimeError)


def test_candidate_does_not_claim_production_ready(composition):
    assert composition.bind_port not in FORBIDDEN_PORTS
    client = composition.test_client()
    status = client.get("/api/v1/status", headers=composition.local_headers())
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["ok"] is True
    gates = body["unmet_gates"]
    assert any("切流" in item for item in gates)
    assert any("isolated candidate" in item for item in gates)
    check = client.get("/api/setup/check", headers=composition.local_headers())
    assert check.status_code == 503, "未注入设置探测时不得返回假就绪"
    assert check.json()["ok"] is False
    openapi = client.get("/openapi.json", headers=composition.local_headers())
    assert openapi.status_code == 200
    assert "/api/v1/utterances" in openapi.json()["paths"]


def test_session_chunks_partial_final_submit_claim_ack(composition):
    client = composition.test_client()
    headers = composition.local_headers()
    created = client.post("/api/v1/recognize/sessions", json={"language": "zh"}, headers=headers)
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    q = composition.app.state.sessions.subscribe(sid)
    chunk = client.post(
        f"/api/v1/recognize/sessions/{sid}/chunks",
        content=_pcm(),
        headers={**headers, SIDECAR_CHUNK_HEADER: "0", "Content-Type": "application/octet-stream"},
    )
    assert chunk.status_code == 202, chunk.text
    finished = client.post(f"/api/v1/recognize/sessions/{sid}/finish", headers=headers)
    assert finished.status_code == 200, finished.text
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    composition.app.state.sessions.unsubscribe(sid, q)
    kinds = [getattr(ev, "event", None) for ev in events]
    assert "partial" in kinds
    partial = next(ev for ev in events if ev.event == "partial")
    assert partial.stable_text == ""
    assert partial.tail_text == "fixture-tail"
    assert "final" in kinds
    final = next(ev for ev in events if ev.event == "final")
    assert final.text == "fixture-final"
    submitted = client.post(
        "/api/v1/utterances",
        json={"job_id": "job-iso", "session_id": sid, "source": "desktop", "focus_id": "focus-A"},
        headers=headers,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["text"] == "fixture-final"
    claimed = composition.jobs_client.claim()
    assert claimed is not None
    assert claimed["job_id"] == "job-iso"
    assert claimed["focus_id"] == "focus-A"
    acked = composition.jobs_client.ack("job-iso", claimed["claim_id"], "complete")
    assert acked["state"] == "done"
    history = client.get("/api/v1/history", headers=headers).json()
    assert history["total"] == 1
    record = history["records"][0]
    # 历史不是输入/投递证明
    assert record["final_text"] == "fixture-final"
    assert "state" not in record or record.get("state") not in {"done", "complete"}
    job = client.get("/api/v1/jobs/job-iso", headers=headers).json()
    assert job["job"]["state"] == "done"


def test_history_without_job_is_failclosed(composition):
    client = composition.test_client()
    headers = composition.local_headers()
    sid = client.post("/api/v1/recognize/sessions", json={"language": "zh"}, headers=headers).json()["session_id"]
    client.post(
        f"/api/v1/recognize/sessions/{sid}/chunks",
        content=_pcm(),
        headers={**headers, SIDECAR_CHUNK_HEADER: "0", "Content-Type": "application/octet-stream"},
    )
    client.post(f"/api/v1/recognize/sessions/{sid}/finish", headers=headers)
    first = client.post(
        "/api/v1/utterances",
        json={"job_id": "job-gap", "session_id": sid, "source": "desktop", "focus_id": "f"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    jobs_path = composition.root / "jobs.json"
    jobs_path.write_text("", encoding="utf-8")
    # 同一 payload 再提交：历史在、job 行缺失，必须 fail-closed，不得自动重投
    again = client.post(
        "/api/v1/utterances",
        json={"job_id": "job-gap", "session_id": sid, "source": "desktop", "focus_id": "f"},
        headers=headers,
    )
    assert again.status_code == 409, again.text
    assert "处理历史存在但任务状态缺失" in again.json()["message"]
    assert "禁止自动重投" in again.json()["message"]
    history = client.get("/api/v1/history", headers=headers).json()
    assert history["total"] == 1


def test_from_audio_unknown_resend_needs_original_and_does_not_autoretry(composition):
    client = composition.test_client()
    headers = composition.local_headers()
    created = client.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "job-audio", "language": "zh", "focus_id": "focus-R"},
        files={"audio": ("a.txt", b"once-text", "text/plain")},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    claimed = composition.jobs_client.claim()
    assert claimed["job_id"] == "job-audio"
    unknown = composition.jobs_client.ack("job-audio", claimed["claim_id"], "partial_or_unknown")
    assert unknown["state"] == "delivery_unknown"
    pending = client.post(
        "/api/v1/utterances/resend",
        json={"job_id": "job-new", "text": "once-text", "original_job_id": "job-audio"},
        headers=headers,
    )
    assert pending.status_code == 200, pending.text
    body = pending.json()
    assert body["ok"] is False
    assert body["requires_confirmation"] is True
    assert client.get("/api/v1/jobs/job-new", headers=headers).json()["found"] is False
    confirmed = client.post(
        "/api/v1/utterances/resend",
        json={"job_id": "job-new", "text": "once-text", "original_job_id": "job-audio", "confirm": True},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["ok"] is True
    # 未知结果不得自动再入一队
    listed = [job for job in composition.runtime.jobs._store.list_jobs() if job.state.value == "queued"]
    assert [job.job_id for job in listed] == ["job-new"]


def test_legacy_setup_and_token_paths_removed(composition):
    client = composition.test_client()
    headers = composition.local_headers()
    for path in (
        "/api/setup/pull_model",
        "/api/setup/install_ollama",
        "/api/setup/start_ollama",
        "/api/setup/preload_whisper",
        "/api/setup/delete_whisper",
        "/api/setup/qr",
        "/api/setup/issue_token",
        "/api/v1/token",
    ):
        res = client.post(path, json={}, headers=headers)
        assert res.status_code in {404, 405}, (path, res.text)
    res = client.post("/api/setup/tasks", json={"kind": "ollama_pull"}, headers=headers)
    assert res.status_code == 503 and res.json()["ok"] is False, "未注入真实动作时不得假成功"


def test_platform_adapter_does_not_touch_user_input(composition):
    backend = RecordingBackend()
    from _focus import StaticFocusProbe

    injector = FocusBoundInjector(StaticFocusProbe(7), backend)
    result, evidence = injector.inject("hello", focus_id=7, claim_id="c-iso")
    assert result == "complete"
    assert backend.sent == ["hello"]
    # mismatch 不调用后端，不等于对用户窗口注入
    none, ev = injector.inject("nope", focus_id=8, claim_id="c-iso-2")
    assert none == "none"
    assert ev.proved_none is True
    assert backend.sent == ["hello"]
    # JobsClient 走隔离 app，不连 17945
    assert composition.jobs_client._client.base_url.port not in FORBIDDEN_PORTS


def test_reserved_ports_and_live_paths_rejected(tmp_path: Path):
    try:
        compose_isolated_candidate(
            root=tmp_path / "x",
            token="test-isolated-composition-token",
            bind_port=17945,
            stream_provider=FixtureStreamProvider(),
        )
        raise AssertionError("17945 must be rejected")
    except ValueError as exc:
        assert "17945" in str(exc) or "保留端口" in str(exc)
    try:
        compose_isolated_candidate(
            root=tmp_path / "y",
            token="test-isolated-composition-token",
            ollama_base_url="http://127.0.0.1:11434",
            stream_provider=FixtureStreamProvider(),
        )
        raise AssertionError("11434 must be rejected")
    except ValueError as exc:
        assert "11434" in str(exc) or "保留端口" in str(exc)
    live_history_parent = PROJECT_ROOT / ".cache"
    try:
        compose_isolated_candidate(
            root=live_history_parent,
            token="test-isolated-composition-token",
            stream_provider=FixtureStreamProvider(),
        )
        raise AssertionError("live cache root must be rejected")
    except RuntimeError as exc:
        assert "live" in str(exc).lower() or "拒绝" in str(exc)


def test_http_fake_sidecar_and_static_optional(tmp_path: Path, composition):
    from _asr import AsrSidecarClient
    from _asr_provider import SidecarStreamProvider
    from runtimes.asr.server import create_sidecar_app
    from tests.integration.loopback_asgi import LoopbackASGIServer
    from tests.sidecar.fake_engine import FakeEngine

    engine = FakeEngine(text="sidecar-fixture-final")
    sidecar_http = LoopbackASGIServer(create_sidecar_app(engine))
    sidecar_http.start()
    sidecar = AsrSidecarClient(base_url=sidecar_http.base_url)
    provider = SidecarStreamProvider(sidecar)
    nested = compose_isolated_candidate(
        root=tmp_path / "sidecar-iso",
        token="test-isolated-sidecar-token",
        sidecar_base_url=sidecar_http.base_url,
        sidecar_client=sidecar,
        transcribe=fixture_transcribe,
        stream_provider=provider,
        fixture_notes=("HTTP FakeEngine + AsrSidecarClient 回环，非官方 GPU 引擎",),
        mount_static=True,
    )
    try:
        client = nested.test_client()
        headers = nested.local_headers()
        sid = client.post("/api/v1/recognize/sessions", json={"language": "zh"}, headers=headers).json()["session_id"]
        client.post(
            f"/api/v1/recognize/sessions/{sid}/chunks",
            content=_pcm(),
            headers={**headers, SIDECAR_CHUNK_HEADER: "0", "Content-Type": "application/octet-stream"},
        )
        finish = client.post(f"/api/v1/recognize/sessions/{sid}/finish", headers=headers)
        assert finish.status_code == 200, finish.text
        submitted = client.post(
            "/api/v1/utterances",
            json={"job_id": "job-side", "session_id": sid, "source": "desktop", "focus_id": "f"},
            headers=headers,
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["text"] == "sidecar-fixture-final"
        if nested.static_mounted:
            page = client.get("/voice", headers=headers)
            assert page.status_code == 200, page.text
            assert page.content == (PROJECT_ROOT / "static" / "v3" / "voice.html").read_bytes()
            assert client.get("/static/v3/voice.html", headers=headers).status_code == 404
    finally:
        provider.close()
        nested.close()
        sidecar.close()
        sidecar_http.close()


def test_webkit_missing_new_with_context_fails_closed():
    from _overlay import project_cert_path
    from _webkit_host import WebKitHostError, WebKitStatusHost
    from tests.platform.fake_webkit import FakeGdk, FakeGio, FakeGtk, FakeWebKit2

    class NoContextWebKit(FakeWebKit2):
        class WebView:
            def __init__(self, *a, **k):
                raise AssertionError("不得退回未配置 context 的 WebView()")

    host = WebKitStatusHost(modules=(FakeGio, FakeGtk, NoContextWebKit, FakeGdk), cert=project_cert_path(), injected=True)
    try:
        host.create_status_window()
        raise AssertionError("missing new_with_context must fail")
    except WebKitHostError as exc:
        assert "new_with_context" in str(exc)
