from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from runtimes.asr.server import create_sidecar_app
from tests.sidecar.fake_engine import FakeEngine


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine(text="甚至出现交易几乎停滞的情况。")


@pytest.fixture
def app(engine: FakeEngine):
    return create_sidecar_app(engine)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)
