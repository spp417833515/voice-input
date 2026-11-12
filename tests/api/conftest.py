"""API 层测试夹具。必须显式 sandbox Runtime；禁止指向 live 配置/历史/队列或读取真实 token。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _config import default_config_path
from _contracts import PolishRequest, PolishResponse, SttResponse, TranslateRequest, TranslateResponse
from _jobs import default_jobs_path
from _v3_app import build_sandbox_runtime, create_app
from tests.api.providers import FixtureAsrProvider, fixture_ocr, fixture_transcribe

TEST_TOKEN = "test-api-token-not-for-production"
LOCAL_ORIGIN = "https://127.0.0.1:17945"
LAN_HOST = "192.168.10.20"
LAN_ORIGIN = f"https://{LAN_HOST}:17945"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE_PATHS = {
    "config": default_config_path(),
    "jobs": default_jobs_path(),
    "history": PROJECT_ROOT / ".cache" / "history.jsonl",
    "token": PROJECT_ROOT / ".web_input_token",
}


def _fingerprint(path: Path) -> tuple[bool, int | None, int | None]:
    if not path.exists():
        return (False, None, None)
    st = path.stat()
    return (True, st.st_mtime_ns, st.st_size)


@pytest.fixture(scope="session")
def live_fingerprints() -> dict[str, tuple[bool, int | None, int | None]]:
    return {name: _fingerprint(path) for name, path in LIVE_PATHS.items()}


@pytest.fixture(autouse=True, scope="session")
def _forbid_live_mutation(live_fingerprints):
    yield
    after = {name: _fingerprint(path) for name, path in LIVE_PATHS.items()}
    assert after == live_fingerprints, f"API 测试不得改动 live 文件: {after}"


@pytest.fixture
def runtime(tmp_path: Path):
    def polish(request: PolishRequest) -> PolishResponse:
        return PolishResponse(text=request.text.replace("错字", "正字"))

    def translate(request: TranslateRequest) -> TranslateResponse:
        return TranslateResponse(text=f"[{request.target_lang}]{request.text}", target_lang=request.target_lang)

    return build_sandbox_runtime(
        root=tmp_path / "sandbox",
        token=TEST_TOKEN,
        transcribe=fixture_transcribe,
        polish=polish,
        translate=translate,
        ocr=fixture_ocr,
        stream_provider=FixtureAsrProvider(final_text="fixture-final"),
        unmet_gates=("api tests: sandbox, 非生产",),
    )


@pytest.fixture
def app(runtime):
    return create_app(runtime)


def local_client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000), base_url="https://127.0.0.1:17945")


def lan_client(app, host: str = LAN_HOST) -> TestClient:
    return TestClient(app, client=(host, 50000), base_url=f"https://{host}:17945")


def local_headers() -> dict[str, str]:
    return {"Origin": LOCAL_ORIGIN}


def lan_headers(*, token: str = TEST_TOKEN, host: str = LAN_HOST) -> dict[str, str]:
    headers = {"Origin": f"https://{host}:17945", "Host": f"{host}:17945"}
    if token:
        headers["X-Voice-Token"] = token
    return headers
