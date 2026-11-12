"""逻辑层测试夹具。禁止读写 live .config.json / 私人 history.jsonl / jobs.json。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIVE_CONFIG = ROOT / ".config.json"
LIVE_HISTORY = ROOT / ".cache" / "history.jsonl"
LIVE_JOBS = ROOT / ".cache" / "jobs.json"


def _fingerprint(path: Path) -> tuple[bool, int | None, int | None]:
    if not path.exists():
        return (False, None, None)
    st = path.stat()
    return (True, st.st_mtime_ns, st.st_size)


@pytest.fixture(scope="session")
def live_fingerprints() -> dict[str, tuple[bool, int | None, int | None]]:
    return {
        "config": _fingerprint(LIVE_CONFIG),
        "history": _fingerprint(LIVE_HISTORY),
        "jobs": _fingerprint(LIVE_JOBS),
    }


@pytest.fixture(autouse=True, scope="session")
def _forbid_live_mutation(live_fingerprints):
    yield
    after = {
        "config": _fingerprint(LIVE_CONFIG),
        "history": _fingerprint(LIVE_HISTORY),
        "jobs": _fingerprint(LIVE_JOBS),
    }
    assert after == live_fingerprints, f"逻辑测试不得改动 live 文件: {after} vs {live_fingerprints}"


@pytest.fixture
def tmp_store_dir(tmp_path: Path) -> Path:
    # pytest tmp_path is under the system tmp; also expose a project-local mirror when needed.
    return tmp_path
