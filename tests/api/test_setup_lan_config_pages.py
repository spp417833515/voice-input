"""设置真实状态/作业、局域网签发、配置视图、唯一界面入口与旧路径删除。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _contracts import SetupTaskKind
from _v3.production import STATIC_V3
from _v3_app import build_sandbox_runtime, create_app, export_openapi_document
from tests.api.conftest import TEST_TOKEN, lan_client, lan_headers, local_client, local_headers
from tests.api.setup_fixtures import FAKE_LAN_IPS, FakeWorld, GatedAction, build_setup

LEGACY_PATHS = (
    ("POST", "/api/transcribe"),
    ("POST", "/api/ocr_translate"),
    ("GET", "/api/web_input/config"),
    ("GET", "/api/web_input/info"),
    ("POST", "/api/web_input/submit"),
    ("GET", "/api/web_input/poll"),
    ("POST", "/api/web_input/report"),
    ("GET", "/api/web_input/history"),
    ("POST", "/api/web_input/resend"),
    ("POST", "/api/setup/pull_model"),
    ("POST", "/api/setup/delete_model"),
    ("POST", "/api/setup/install_ollama"),
    ("POST", "/api/setup/start_ollama"),
    ("POST", "/api/setup/preload_whisper"),
    ("POST", "/api/setup/delete_whisper"),
    ("POST", "/api/setup/set_config"),
    ("GET", "/history"),
    ("GET", "/voice_setup"),
    ("GET", "/api_docs"),
    ("GET", "/static/setup.html"),
    ("GET", "/static/voice.html"),
    ("GET", "/static/v3/setup.html"),
)


def _app(tmp_path: Path, *, world: FakeWorld | None = None, actions=None, lan=True, static=True):
    runtime = build_sandbox_runtime(
        root=tmp_path / "sb",
        token=TEST_TOKEN,
        setup=None if world is None else build_setup(world, actions),
        lan_ips=(lambda: list(FAKE_LAN_IPS)) if lan else None,
    )
    return runtime, create_app(runtime, static_v3=STATIC_V3 if static else None)


def _wait(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等待超时")


# ---------------------------------------------------------------- setup/check


def test_setup_check_without_injection_is_visible_failure(tmp_path):
    _rt, app = _app(tmp_path)
    res = local_client(app).get("/api/setup/check")
    assert res.status_code == 503
    assert res.json()["ok"] is False


def test_setup_check_reports_missing_with_actions_and_wayland_portal(tmp_path):
    world = FakeWorld()
    _rt, app = _app(tmp_path, world=world)
    body = local_client(app).get("/api/setup/check").json()
    assert body["session_type"] == "wayland"
    assert body["asr"]["status"] == "missing"
    assert body["asr"]["action"] == "asr_download"
    assert "config.json" in body["asr"]["message"]
    assert body["ollama"]["running"] is True and body["ollama"]["model_status"] == "missing"
    assert body["ollama"]["action"] == "ollama_pull"
    caps = {c["id"]: c for c in body["capabilities"]}
    assert caps["portal_screenshot"]["available"] and caps["portal_screenshot"]["required"]
    assert caps["portal_shortcuts"]["available"]
    assert "maim" not in caps
    assert caps["xclip"]["available"] and not caps["notify-send"]["available"]
    assert caps["ocr_chi_sim"]["available"] and not caps["ocr_jpn"]["available"] and not caps["ocr_jpn"]["required"]
    assert body["unmet_gates"] == ["fixture gate"]


def test_setup_check_x11_and_unknown_session(tmp_path):
    world = FakeWorld(session="x11", tools={"xclip": "/usr/bin/xclip", "maim": "/usr/bin/maim"})
    _rt, app = _app(tmp_path, world=world)
    caps = {c["id"]: c for c in local_client(app).get("/api/setup/check").json()["capabilities"]}
    assert caps["maim"]["available"] and caps["maim"]["required"]
    assert "portal_screenshot" not in caps
    world.session = "unknown"
    caps = {c["id"]: c for c in local_client(app).get("/api/setup/check").json()["capabilities"]}
    assert caps["session_type"]["available"] is False
    assert not caps["portal_screenshot"]["required"] and not caps["maim"]["required"]


def test_ollama_not_installed_then_not_running(tmp_path):
    from _v3.setup_probe import OllamaProbe

    world = FakeWorld(ollama=OllamaProbe(False, False, None, False, None, "Ollama 未响应: refused"))
    _rt, app = _app(tmp_path, world=world)
    client = local_client(app)
    assert client.get("/api/setup/check").json()["ollama"]["action"] == "ollama_install"
    world.ollama = OllamaProbe(True, False, None, False, None, "Ollama 未响应: refused")
    body = client.get("/api/setup/check").json()["ollama"]
    assert body["action"] == "ollama_start" and "refused" in body["message"]


def test_download_task_progress_then_ready_never_fake(tmp_path):
    from _v3.setup_probe import AsrWeightsProbe

    world = FakeWorld()

    def weights_arrive():
        world.weights = AsrWeightsProbe(True, 4_400_000_000, ())

    action = GatedAction(on_success=weights_arrive)
    _rt, app = _app(tmp_path, world=world, actions={SetupTaskKind.asr_download: action})
    client = local_client(app)
    started = client.post("/api/setup/tasks", json={"kind": "asr_download"}, headers=local_headers())
    assert started.status_code == 202, started.text
    task = started.json()
    assert task["state"] == "running"
    assert action.reported.wait(5)
    again = client.post("/api/setup/tasks", json={"kind": "asr_download"}, headers=local_headers()).json()
    assert again["task_id"] == task["task_id"], "运行中的同类作业不得重复启动"
    check = client.get("/api/setup/check").json()["asr"]
    assert check["status"] == "downloading"
    assert check["progress"]["percent"] == 25.0 and check["task_id"] == task["task_id"]
    action.release.set()
    done = _wait(lambda: (r := client.get(f"/api/setup/tasks/{task['task_id']}").json())["state"] != "running" and r)
    assert done["state"] == "succeeded"
    asr = client.get("/api/setup/check").json()["asr"]
    assert asr["status"] == "ready" and asr["weights_present"] is True
    assert asr["sidecar_running"] is False and asr["action"] == "asr_start"
    assert action.calls == 1


def test_failed_task_is_visible_with_retry_action(tmp_path):
    action = GatedAction(fail="网络被拒绝: fixture")
    world = FakeWorld()
    _rt, app = _app(tmp_path, world=world, actions={SetupTaskKind.ollama_pull: action})
    client = local_client(app)
    task = client.post("/api/setup/tasks", json={"kind": "ollama_pull"}, headers=local_headers()).json()
    assert client.get("/api/setup/check").json()["ollama"]["model_status"] == "downloading"
    action.release.set()
    done = _wait(lambda: (r := client.get(f"/api/setup/tasks/{task['task_id']}").json())["state"] != "running" and r)
    assert done["state"] == "failed" and done["error"] == "dependency_failed"
    assert "fixture" in done["message"]
    ollama = client.get("/api/setup/check").json()["ollama"]
    assert ollama["model_status"] == "failed" and ollama["action"] == "ollama_pull"
    assert ollama["task_id"] == task["task_id"]


def test_unprovided_action_and_unknown_task_are_not_success(tmp_path):
    _rt, app = _app(tmp_path, world=FakeWorld())
    client = local_client(app)
    res = client.post("/api/setup/tasks", json={"kind": "ollama_install"}, headers=local_headers())
    assert res.status_code == 503 and res.json()["ok"] is False
    assert client.get("/api/setup/tasks/nope").status_code == 404
    bad = client.post("/api/setup/tasks", json={"kind": "preload_whisper"}, headers=local_headers())
    assert bad.status_code == 400


def test_setup_routes_are_local_only(tmp_path):
    _rt, app = _app(tmp_path, world=FakeWorld())
    remote = lan_client(app)
    assert remote.get("/api/setup/check", headers=lan_headers()).status_code == 403
    assert remote.post("/api/setup/tasks", json={"kind": "asr_download"}, headers=lan_headers()).status_code == 403


# ---------------------------------------------------------------- LAN access


def test_lan_access_local_issues_entries_qr_and_token(tmp_path):
    _rt, app = _app(tmp_path)
    body = local_client(app).get("/api/v1/lan-access").json()
    assert body["port"] == 17945 and body["token"] == TEST_TOKEN
    assert TEST_TOKEN not in body["token_masked"]
    assert [e["ip"] for e in body["entries"]] == FAKE_LAN_IPS
    first = body["entries"][0]
    assert first["url"] == f"https://192.168.10.20:17945/voice?token={TEST_TOKEN}"
    assert TEST_TOKEN not in first["url_masked"]
    assert "<svg" in first["qr_svg"]


def test_lan_access_forbidden_remotely_even_with_token(tmp_path):
    _rt, app = _app(tmp_path)
    assert lan_client(app).get("/api/v1/lan-access", headers=lan_headers()).status_code == 403
    public = TestClient(app, client=("8.8.8.8", 5000), base_url="https://8.8.8.8:17945")
    assert public.get("/api/v1/lan-access", headers={"X-Voice-Token": TEST_TOKEN}).status_code == 403


def test_lan_access_without_provider_is_visible_failure(tmp_path):
    _rt, app = _app(tmp_path, lan=False)
    res = local_client(app).get("/api/v1/lan-access")
    assert res.status_code == 503 and res.json()["ok"] is False


def test_lan_from_audio_needs_token(tmp_path):
    _rt, app = _app(tmp_path)
    remote = lan_client(app)
    no_token = remote.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "j"},
        files={"audio": ("a.wav", b"x", "audio/wav")},
        headers=lan_headers(token=""),
    )
    assert no_token.status_code == 403


# ---------------------------------------------------------------- config view


def test_config_api_never_returns_legacy_secrets(tmp_path):
    runtime, app = _app(tmp_path)
    cfg_path = runtime.config.path
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data.update({"baidu_appid": "fixture-appid", "baidu_appkey": "fixture-secret", "whisper_model": "large-v3"})
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    client = local_client(app)
    got = client.get("/api/v1/config").json()
    assert "fixture-secret" not in json.dumps(got) and "baidu_appkey" not in got and "whisper_model" not in got
    saved = client.post(
        "/api/v1/config",
        json={"custom_keywords": [" Alpha ", "", "Beta"], "hotkey_voice": "Control+Alt+Z", "polish_enabled": True},
        headers=local_headers(),
    )
    assert saved.status_code == 200, saved.text
    out = saved.json()
    assert out["custom_keywords"] == ["Alpha", "Beta"] and out["hotkey_voice"] == "ctrl+alt+z"
    assert out["polish_enabled"] is True and "baidu_appkey" not in out
    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert disk["baidu_appkey"] == "fixture-secret", "旧键在磁盘保留，不丢数据"
    rejected = client.post("/api/v1/config", json={"baidu_appkey": "x"}, headers=local_headers())
    assert rejected.status_code == 400
    bad_key = client.post("/api/v1/config", json={"hotkey_voice": "z"}, headers=local_headers())
    assert bad_key.status_code == 400


# ---------------------------------------------------------------- pages


def test_pages_come_only_from_static_v3(tmp_path):
    _rt, app = _app(tmp_path)
    client = local_client(app)
    for url, name in (("/setup", "setup.html"), ("/voice", "voice.html"),
                      ("/overlay-status", "overlay-status.html"), ("/overlay-translate", "overlay-translate.html")):
        res = client.get(url)
        assert res.status_code == 200, url
        assert res.content == (STATIC_V3 / name).read_bytes()
        assert res.headers["referrer-policy"] == "no-referrer"
        assert b"Whisper" not in res.content and "百度".encode() not in res.content
    assert client.get("/js/business/http.js").status_code == 200
    assert client.get("/css/theme.css").status_code == 200
    snap = client.get("/openapi.snapshot.json")
    assert snap.status_code == 200 and snap.json()["paths"]


def test_phone_page_reachable_from_lan_but_setup_is_local(tmp_path):
    _rt, app = _app(tmp_path)
    remote = lan_client(app)
    lan_host = {"Host": "192.168.10.20:17945"}
    assert remote.get("/voice", headers=lan_host).status_code == 200
    assert remote.get("/js/voice.js", headers=lan_host).status_code == 200
    assert remote.get("/openapi.snapshot.json", headers=lan_host).status_code == 200
    assert remote.get("/setup", headers=lan_host).status_code == 403
    assert remote.get("/overlay-status", headers=lan_host).status_code == 403
    assert remote.get("/openapi.json", headers=lan_host).status_code == 403
    public = TestClient(app, client=("8.8.8.8", 5000), base_url="https://8.8.8.8:17945")
    assert public.get("/voice", headers={"Host": "8.8.8.8:17945"}).status_code == 403


@pytest.mark.parametrize("method,path", LEGACY_PATHS)
def test_legacy_paths_are_gone_without_redirect(tmp_path, method, path):
    _rt, app = _app(tmp_path, world=FakeWorld())
    client = TestClient(app, client=("127.0.0.1", 50000), base_url="https://127.0.0.1:17945", follow_redirects=False)
    res = client.request(method, path, headers=local_headers())
    assert res.status_code in {404, 405}, (path, res.status_code)
    assert "location" not in res.headers


def test_openapi_snapshot_is_the_generated_document(tmp_path):
    generated = export_openapi_document(root=tmp_path / "oa")
    snapshot = json.loads((STATIC_V3 / "openapi.json").read_text(encoding="utf-8"))
    assert snapshot == generated, "static/v3/openapi.json 必须由 scripts/gate.py --write-openapi-snapshot 生成"
    assert "/api/v1/lan-access" in snapshot["paths"]
    assert "/api/setup/tasks" in snapshot["paths"]
    for schema in ("SetupCheckResponse", "SetupTask", "LanAccessResponse", "AppConfigView", "AppConfigUpdate"):
        assert schema in snapshot["components"]["schemas"]
    assert TEST_TOKEN not in json.dumps(snapshot)
