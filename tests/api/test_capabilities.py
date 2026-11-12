from __future__ import annotations

from pathlib import Path

from _v3_app import build_sandbox_runtime, create_app
from tests.api.conftest import TEST_TOKEN, lan_client, lan_headers, local_client, local_headers
from tests.api.providers import fixture_transcribe


def test_public_stt_does_not_write_history_or_queue(app, runtime):
    client = local_client(app)
    res = client.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("a.txt", b"only-stt", "application/octet-stream")},
        headers=local_headers(),
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["text"] == "only-stt"
    assert runtime.history.list_records().total == 0
    assert runtime.jobs.get("anything").found is False


def test_stt_silence_and_missing_audio_are_400(app):
    client = local_client(app)
    silent = client.post(
        "/api/v1/stt",
        files={"audio": ("s.pcm", b"\x00\x00\x00\x00", "application/octet-stream")},
        headers=local_headers(),
    )
    assert silent.status_code == 400
    missing = client.post("/api/v1/stt", data={"language": "zh"}, headers=local_headers())
    assert missing.status_code == 400


def test_polish_and_translate_use_injected_capabilities(app):
    client = local_client(app)
    polish = client.post("/api/v1/polish", json={"text": "有错字", "language": "zh"}, headers=local_headers())
    assert polish.status_code == 200
    assert polish.json()["text"] == "有正字"
    trans = client.post("/api/v1/translate", json={"text": "hello", "target_lang": "zh"}, headers=local_headers())
    assert trans.status_code == 200
    assert trans.json()["text"] == "[zh]hello"
    assert trans.json()["text"] != "hello" or trans.json()["target_lang"] == "zh"


def test_missing_polish_capability_is_structured_error(tmp_path: Path):
    runtime = build_sandbox_runtime(
        root=tmp_path / "no-polish",
        token=TEST_TOKEN,
        transcribe=fixture_transcribe,
        polish=None,
    )
    app = create_app(runtime)
    client = local_client(app)
    res = client.post("/api/v1/polish", json={"text": "原文"}, headers=local_headers())
    assert res.status_code == 503
    body = res.json()
    assert body["error"] == "polish_failed"
    assert body["ok"] is False
    assert body.get("text") != "原文"


def test_ocr_translate_raw_and_translated(app):
    client = local_client(app)
    raw = client.post(
        "/api/v1/ocr-translate",
        data={"target_lang": "raw"},
        files={"image": ("t.txt", "画面文字".encode("utf-8"), "text/plain")},
        headers=local_headers(),
    )
    assert raw.status_code == 200
    assert raw.json()["ocr_text"] == "画面文字"
    assert raw.json()["text"] == "画面文字"
    translated = client.post(
        "/api/v1/ocr-translate",
        data={"target_lang": "en"},
        files={"image": ("t.txt", "画面文字".encode("utf-8"), "text/plain")},
        headers=local_headers(),
    )
    assert translated.status_code == 200
    assert translated.json()["ocr_text"] == "画面文字"
    assert translated.json()["text"] == "[en]画面文字"
    assert translated.json()["text"] != translated.json()["ocr_text"]


def test_ocr_empty_is_error(app):
    client = local_client(app)
    res = client.post(
        "/api/v1/ocr-translate",
        data={"target_lang": "en"},
        files={"image": ("t.txt", b"empty-image", "text/plain")},
        headers=local_headers(),
    )
    assert res.status_code == 503
    assert res.json()["error"] == "ocr_failed"


def test_lan_can_call_stt_and_cannot_call_ocr(app):
    client = lan_client(app)
    stt = client.post(
        "/api/v1/stt",
        files={"audio": ("a.txt", b"lan-hi", "application/octet-stream")},
        headers=lan_headers(),
    )
    assert stt.status_code == 200
    ocr = client.post(
        "/api/v1/ocr-translate",
        data={"target_lang": "raw"},
        files={"image": ("t.txt", "画面文字".encode("utf-8"), "text/plain")},
        headers=lan_headers(),
    )
    assert ocr.status_code == 403
