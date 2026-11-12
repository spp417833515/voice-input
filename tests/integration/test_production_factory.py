"""生产工厂隔离装配：同一 compose_production_runtime + create_app(static/v3)，

隔离根目录 + 假令牌 + httpx.MockTransport（假 Ollama / 假 sidecar 主机名，不开任何套接字）+ 假进程。
验证：启动恢复、配置迁移、真实适配器接线、设置作业、局域网签发、激活门闩。不是官方模型或真机验收。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from _asr_provider import SidecarStreamProvider
from _v3.production import (
    CODE_ROOT,
    STATIC_V3,
    ProductionEndpoints,
    ProductionPaths,
    SystemAdapters,
    compose_production_runtime,
    require_activation_allowed,
)
from _v3.runtime import UnmetRuntimeGates as Gates
from _v3_app import create_app

FAKE_TOKEN = "test-production-factory-token"
ENDPOINTS = ProductionEndpoints(sidecar_url="http://sidecar.fixture", ollama_url="http://ollama.fixture")


class FakeServices:
    """假 Ollama + 假 ASR sidecar。记录请求，供断言真实适配器的协议。"""

    def __init__(self) -> None:
        self.models: list[str] = []
        self.sidecar_loaded = False
        self.requests: list[tuple[str, str]] = []
        self.chat_payloads: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.requests.append((host, path))
        if host == "ollama.fixture":
            if path == "/api/version":
                return httpx.Response(200, json={"version": "0.40.16"})
            if path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": m, "model": m, "size": 3_300_000_000} for m in self.models]})
            if path == "/api/pull":
                self.models.append(json.loads(request.content)["model"])
                lines = [{"status": "pulling x", "digest": "d", "total": 10, "completed": 10}, {"status": "success"}]
                return httpx.Response(200, content="\n".join(map(json.dumps, lines)).encode())
            if path == "/api/chat":
                payload = json.loads(request.content)
                self.chat_payloads.append(payload)
                text = payload["messages"][-1]["content"].split("\n")[-1].replace("错字", "正字")
                return httpx.Response(200, json={"message": {"content": text}})
        if host == "sidecar.fixture":
            if path == "/health":
                return httpx.Response(200, json={"ok": True, "loaded": self.sidecar_loaded})
            if path == "/v1/transcribe":
                return httpx.Response(200, json={"ok": True, "text": "今天天气错字", "language": "zh"})
        return httpx.Response(404, json={"error": f"fixture 未定义 {host}{path}"})


class FakeRun:
    def __init__(self, services: FakeServices) -> None:
        self.calls: list[list[str]] = []
        self.services = services

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        if cmd[:3] == ["systemctl", "--user", "start"] and cmd[3] == "ollama-voice-asr.service":
            self.services.sidecar_loaded = True
        if cmd[:3] == ["systemctl", "--user", "show"]:
            # start_action 先后读单元真实状态；假单元在 start 后 active（就绪仍由 sidecar /health 判定）
            started = ["systemctl", "--user", "start", cmd[3]] in self.calls
            state = "ActiveState=active\nSubState=running\n" if started else "ActiveState=inactive\nSubState=dead\n"
            exec_start = (
                "ExecStart={ path=/v/bin/python ; argv[]=/v/bin/python -m runtimes.asr serve --host 127.0.0.1 "
                "--port 17946 --load --warmup --model /m/Qwen3-ASR-1.7B ; pid=0 ; code=(null) ; status=0/0 }\n"
                if cmd[3] == "ollama-voice-asr.service" else ""
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=(
                f"LoadState=loaded\n{state}Result=success\nConditionResult=yes\n"
                f"ConditionTimestampMonotonic={2 if started else 1}\nExecMainCode=0\nExecMainStatus=0\nNRestarts=0\n"
                f"{exec_start}DropInPaths=\n"
            ), stderr="")
        if cmd[0] == "nvidia-smi":  # 假单卡读数：16303 MiB 总量、14574 MiB 空闲（本机 06:3x EST 只读读数）
            return subprocess.CompletedProcess(cmd, 0, stdout="0, GPU-fixture, 16303, 14574, Fixture GPU\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _seed(root: Path) -> None:
    (root / ".cache").mkdir(parents=True)
    (root / ".config.json").write_text(json.dumps({
        "ollama_model": "qwen2.5:7b",
        "hotkey_voice": "ctrl+alt+z",
        "custom_keywords": ["专名甲"],
        "translate_target_lang": "en",
        "baidu_appid": "fixture-appid",
        "baidu_appkey": "fixture-secret",
        "whisper_model": "large-v3",
    }), encoding="utf-8")
    claimed = {
        "job_id": "job-claimed", "state": "claimed", "text": "旧句", "source": "desktop", "focus_id": "x11:1",
        "claim_id": "c1", "created_at": "2026-11-10T00:00:00+00:00", "updated_at": "2026-11-10T00:00:00+00:00",
    }
    (root / ".cache" / "jobs.json").write_text(json.dumps({"version": 1, "jobs": {"job-claimed": claimed}}), encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path):
    root = tmp_path / "prod-root"
    _seed(root)
    services = FakeServices()
    run = FakeRun(services)
    adapters = SystemAdapters(
        environ={"XDG_SESSION_TYPE": "wayland"},
        run=run,
        popen=lambda *a, **k: (_ for _ in ()).throw(AssertionError("测试不得启动下载进程")),
        which=lambda name: f"/usr/bin/{name}" if name in ("xclip", "nvidia-smi") else None,
        portal_version=lambda iface: (1, None),
        ocr_languages=lambda: ["eng", "chi_sim"],
        list_ips=lambda: ["192.168.10.20"],
        http_transport=httpx.MockTransport(services),
        spawn=lambda fn: fn(),
    )
    paths = ProductionPaths.for_root(root, live=False)
    runtime = compose_production_runtime(paths=paths, token=FAKE_TOKEN, adapters=adapters, endpoints=ENDPOINTS)
    app = create_app(runtime, static_v3=STATIC_V3)
    client = TestClient(app, client=("127.0.0.1", 50000), base_url="https://127.0.0.1:17945")
    yield {"root": root, "runtime": runtime, "client": client, "services": services, "run": run, "paths": paths}
    client.close()


H = {"Origin": "https://127.0.0.1:17945"}


def test_live_root_refused_without_explicit_authorization():
    with pytest.raises(RuntimeError):
        ProductionPaths.for_root(CODE_ROOT, live=False)
    with pytest.raises(TypeError):
        compose_production_runtime()  # type: ignore[call-arg]


def test_startup_recovers_claimed_jobs_without_redelivery(world):
    doc = json.loads((world["root"] / ".cache" / "jobs.json").read_text(encoding="utf-8"))
    job = doc["jobs"]["job-claimed"]
    assert job["state"] == "delivery_unknown"
    claim = world["client"].post("/api/v1/jobs/claim", json={}, headers=H)
    assert claim.status_code == 200
    assert not claim.json() or claim.json().get("job_id") != "job-claimed", "恢复后不得自动重投"


def test_config_migrated_keeps_user_fields_and_hides_secrets(world):
    disk = json.loads((world["root"] / ".config.json").read_text(encoding="utf-8"))
    assert disk["ollama_model"] == "qwen3.5:4b" and disk["legacy_ollama_model"] == "qwen2.5:7b"
    assert disk["custom_keywords"] == ["专名甲"] and disk["translate_target_lang"] == "en"
    assert disk["baidu_appkey"] == "fixture-secret" and disk["hotkey_screenshot"] == "ctrl+alt+x"
    view = world["client"].get("/api/v1/config").json()
    assert "fixture-secret" not in json.dumps(view) and view["ollama_model"] == "qwen3.5:4b"


def test_real_adapters_drive_from_audio_polish_and_queue(world):
    client = world["client"]
    assert client.post("/api/v1/config", json={"polish_enabled": True}, headers=H).status_code == 200
    res = client.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "job-web-1"},
        files={"audio": ("a.wav", b"RIFF-fixture-bytes", "audio/wav")},
        headers=H,
    )
    assert res.status_code == 200, res.text
    assert res.json()["text"] == "今天天气正字"
    payload = world["services"].chat_payloads[-1]
    assert payload["model"] == "qwen3.5:4b" and payload["think"] is False
    assert "专名甲" in payload["messages"][-1]["content"]
    assert ("sidecar.fixture", "/v1/transcribe") in world["services"].requests
    assert isinstance(world["runtime"].stream_provider, SidecarStreamProvider)
    stt = client.post("/api/v1/stt", files={"audio": ("a.wav", b"RIFF-x", "audio/wav")}, headers=H)
    assert stt.json()["text"] == "今天天气错字", "公开 STT 不润色"


def test_setup_check_and_real_actions_via_fakes(world):
    client = world["client"]
    check = client.get("/api/setup/check").json()
    assert check["session_type"] == "wayland"
    assert check["ollama"]["running"] and check["ollama"]["model_status"] == "missing"
    assert check["ollama"]["action"] == "ollama_pull"
    assert check["asr"]["status"] == "missing" and check["asr"]["action"] == "asr_download"
    assert check["unmet_gates"], "官方门闩仍需显示"
    pulled = client.post("/api/setup/tasks", json={"kind": "ollama_pull"}, headers=H).json()
    assert pulled["state"] == "succeeded", pulled
    assert client.get("/api/setup/check").json()["ollama"]["model_status"] == "ready"
    caps = {c["id"]: c for c in check["capabilities"]}
    assert caps["hotkey_conflicts"]["available"] is False and "gsettings 不可用" in caps["hotkey_conflicts"]["detail"]
    assert caps["desktop_daemon"]["available"] is False and "./start.sh --acceptance" in caps["desktop_daemon"]["detail"]
    started = client.post("/api/setup/tasks", json={"kind": "asr_start"}, headers=H).json()
    assert started["state"] == "succeeded", started
    calls = world["run"].calls
    smi = next(i for i, c in enumerate(calls) if c[0] == "nvidia-smi")
    assert smi < calls.index(["systemctl", "--user", "start", "ollama-voice-asr.service"]), "预检必须先于 start"


def test_lan_access_uses_explicit_token(world):
    body = world["client"].get("/api/v1/lan-access").json()
    assert body["token"] == FAKE_TOKEN
    assert body["entries"][0]["url"].startswith("https://192.168.10.20:17945/voice?token=")


def test_pages_served_and_activation_refused_until_gates_close(world):
    assert world["client"].get("/setup").status_code == 200
    with pytest.raises(Gates) as ei:
        require_activation_allowed(world["runtime"])
    joined = " ".join(ei.value.gates)
    # P2 已由 r9 证据关闭；4b（P3/P4）与真机门闩必须仍在，生产工厂照样拒绝激活
    assert "P2" not in joined and "qwen3.5:4b" in joined and "真机" in joined
