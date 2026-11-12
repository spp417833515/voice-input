from __future__ import annotations

import json
from pathlib import Path

import pytest

from _config import ConfigStore, default_config_path
from _contracts import LogicError


def test_default_path_is_project_config_but_tests_do_not_write_it():
    path = default_config_path()
    assert path.name == ".config.json"
    assert path.parent == Path(__file__).resolve().parents[2]


def test_missing_file_returns_defaults_without_creating(tmp_path: Path):
    path = tmp_path / "missing.json"
    store = ConfigStore(path)
    cfg = store.load()
    assert cfg.ollama_model == "qwen3.5:4b"
    assert not path.exists()


def test_roundtrip_preserves_unknown_and_old_fields(tmp_path: Path):
    path = tmp_path / ".config.json"
    path.write_text(
        json.dumps(
            {
                "ollama_model": "kept-model",
                "whisper_model": "small",
                "whisper_language": "zh",
                "custom_keywords": ["Foo"],
                "vision_model": "legacy-vision",
                "voice_mode": "push",
                "unspecified_legacy": {"a": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = ConfigStore(path)
    store.update(translate_target_lang="en")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ollama_model"] == "kept-model"
    assert data["translate_target_lang"] == "en"
    assert data["vision_model"] == "legacy-vision"
    assert data["voice_mode"] == "push"
    assert data["unspecified_legacy"] == {"a": 1}
    assert data["custom_keywords"] == ["Foo"]


def test_corrupt_json_is_visible(tmp_path: Path):
    path = tmp_path / ".config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LogicError) as ei:
        ConfigStore(path).load()
    assert "JSON" in ei.value.message
    assert ei.value.code.value == "config_io"


def test_concurrent_updates_do_not_drop_fields(tmp_path: Path):
    import threading

    path = tmp_path / ".config.json"
    path.write_text(
        json.dumps({"ollama_model": "kept-model", "custom_keywords": []}),
        encoding="utf-8",
    )
    # 两个实例必须共享同一路径锁，否则会证明锁登记不正确。
    left = ConfigStore(path)
    right = ConfigStore(path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def set_lang() -> None:
        try:
            barrier.wait()
            left.update(translate_target_lang="en")
        except BaseException as exc:  # noqa: BLE001 — 收集后断言
            errors.append(exc)

    def set_keywords() -> None:
        try:
            barrier.wait()
            right.update(custom_keywords=["Foo"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=set_lang)
    t2 = threading.Thread(target=set_keywords)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ollama_model"] == "kept-model"
    assert data["translate_target_lang"] == "en"
    assert data["custom_keywords"] == ["Foo"]
