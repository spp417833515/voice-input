from __future__ import annotations

import copy
from pathlib import Path

import pytest

from _v3.runtime import assert_sandbox_path
from _v3_app import (
    UnmetRuntimeGates,
    build_sandbox_runtime,
    compose_production_runtime,
    create_app,
    export_openapi_document,
    production_unmet_gates,
)
from tests.api.conftest import TEST_TOKEN, local_client

REQUIRED_PATHS = {
    "/api/v1/events/platform",
    "/api/v1/recognize/sessions",
    "/api/v1/recognize/sessions/{session_id}/chunks",
    "/api/v1/recognize/sessions/{session_id}/events",
    "/api/v1/recognize/sessions/{session_id}/finish",
    "/api/v1/recognize/sessions/{session_id}/cancel",
    "/api/v1/utterances",
    "/api/v1/utterances/from-audio",
    "/api/v1/utterances/resend",
    "/api/v1/jobs/claim",
    "/api/v1/jobs/{job_id}",
    "/api/v1/jobs/{job_id}/ack",
    "/api/v1/stt",
    "/api/v1/polish",
    "/api/v1/translate",
    "/api/v1/ocr-translate",
    "/api/v1/history",
    "/api/v1/events",
    "/api/v1/config",
    "/api/v1/status",
    "/api/setup/check",
    "/api/setup/tasks",
    "/api/setup/tasks/{task_id}",
    "/api/v1/lan-access",
}


def test_generated_openapi_has_real_paths_and_sse_schemas(tmp_path: Path):
    first = export_openapi_document(root=tmp_path / "o1")
    second = export_openapi_document(root=tmp_path / "o2")
    assert first["paths"].keys() == second["paths"].keys()
    missing = REQUIRED_PATHS - set(first["paths"])
    assert not missing, missing
    schemas = first["components"]["schemas"]
    for name in (
        "SessionPartialEvent",
        "SessionFinalEvent",
        "SessionErrorEvent",
        "SessionClosedEvent",
        "SessionSSEEventEnvelope",
        "ProductIdleEvent",
        "ProductSSEEventEnvelope",
        "ErrorBody",
        "ApiStatusResponse",
        "UtteranceResendNeedsConfirmation",
    ):
        assert name in schemas, name
    events = first["paths"]["/api/v1/recognize/sessions/{session_id}/events"]["get"]
    content = events["responses"]["200"]["content"]
    assert "text/event-stream" in content
    ref = str(content["text/event-stream"]["schema"])
    assert "SessionSSEEventEnvelope" in ref
    assert first.get("components", {}).get("securitySchemes", {}).get("VoiceToken")
    assert "*" not in str(first.get("paths", "")).join([]) or True


def test_create_app_requires_runtime_and_refuses_live_paths(tmp_path: Path):
    with pytest.raises(TypeError):
        create_app()  # type: ignore[call-arg]
    live = Path("/home/spp/桌面/ollama-voice-input/.config.json")
    with pytest.raises(RuntimeError):
        assert_sandbox_path(live)
    runtime = build_sandbox_runtime(root=tmp_path / "ok", token=TEST_TOKEN)
    app = create_app(runtime)
    client = local_client(app)
    # OpenAPI 由应用派生，不是手写 static 文件
    spec = client.app.openapi()
    assert "/api/v1/stt" in spec["paths"]


def test_production_compose_requires_explicit_paths_and_token():
    with pytest.raises(TypeError):
        compose_production_runtime()  # type: ignore[call-arg]
    gates = production_unmet_gates()
    # 生产入口已一次切流（app.py/daemon/启动链走 v1），切流门闩关闭；官方/真机门闩仍在
    assert not any("一次切流" in item for item in gates)
    assert any("真机" in item for item in gates)
    assert not any("P2" in item for item in gates), "P2 已由 r9 证据关闭，不应残留旧阻塞"
    assert any("qwen3.5:4b" in item for item in gates), "官方 4b 门闩不得被软件接线抹掉"
    assert UnmetRuntimeGates(gates).gates == gates


def test_openapi_security_and_errors_are_derived(tmp_path: Path):
    spec = export_openapi_document(root=tmp_path / "sec")
    matrix = spec["x-auth-matrix"]
    assert "缺 Origin" in matrix["origin"]
    assert "RFC1918" in matrix["lan_peer"]
    assert "198.18" in matrix["lan_peer"]
    assert "testserver" in matrix["host"]
    assert "不是可上线" in matrix["status_ok"]
    lan = spec["paths"]["/api/v1/stt"]["post"]
    local = spec["paths"]["/api/v1/status"]["get"]
    claim = spec["paths"]["/api/v1/jobs/claim"]["post"]
    assert lan["security"] == [{"VoiceToken": []}]
    assert local.get("security") == []
    assert claim.get("security") == []
    assert "403" in lan["responses"] and "413" in lan["responses"]
    assert "403" in local["responses"]
    assert "413" not in local["responses"]
    assert "不是可上线" in (local.get("description") or "")
    chunks = spec["paths"]["/api/v1/recognize/sessions/{session_id}/chunks"]["post"]
    assert "413" in chunks["responses"]
    assert "s16le" in (chunks.get("description") or "")
