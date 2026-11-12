"""B1：ASGI receive 层按累计实际字节限流。用原始 ASGI，不灌巨大内存。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from _v3_app import build_sandbox_runtime, create_app
from tests.api.asgi_client import call_asgi, multipart
from tests.api.conftest import TEST_TOKEN
from tests.api.providers import FixtureAsrProvider, fixture_ocr, fixture_transcribe

LOCAL = ("127.0.0.1", 50000)


@pytest.fixture
def small_app(tmp_path: Path):
    runtime = build_sandbox_runtime(
        root=tmp_path / "sandbox",
        token=TEST_TOKEN,
        transcribe=fixture_transcribe,
        ocr=fixture_ocr,
        stream_provider=FixtureAsrProvider(),
        max_chunk_bytes=64,
        max_upload_bytes=256,
    )
    return create_app(runtime), runtime


def _local_headers(**extra: str) -> dict[str, str]:
    headers = {"host": "127.0.0.1:17945", "origin": "https://127.0.0.1:17945"}
    for key, value in extra.items():
        headers[key.replace("_", "-")] = value
    return headers


def _session(app) -> str:
    created = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path="/api/v1/recognize/sessions",
            client=LOCAL,
            headers=_local_headers(content_type="application/json"),
            body=b'{"language":"zh"}',
        )
    )
    assert created["status"] == 200, created
    return created["body"]["session_id"]


def test_chunk_without_content_length_oversize_is_413(small_app):
    app, _runtime = small_app
    sid = _session(app)
    result = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/chunks",
            client=LOCAL,
            headers=_local_headers(content_type="application/octet-stream", **{"x-chunk-index": "0"}),
            body=b"\x01\x00" * 40,
            add_content_length=False,
            chunked=True,
        )
    )
    assert result["status"] == 413, result
    assert result["body"]["error"] == "invalid_request"
    assert "超过" in result["body"]["message"]


def test_chunk_content_length_lie_is_413(small_app):
    app, _runtime = small_app
    sid = _session(app)
    result = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/chunks",
            client=LOCAL,
            headers=_local_headers(content_type="application/octet-stream", **{"x-chunk-index": "0"}),
            body=b"\x01\x00" * 40,
            add_content_length=False,
            content_length="5",
        )
    )
    assert result["status"] == 413, result
    assert result["body"]["error"] == "invalid_request"


def test_declared_content_length_over_limit_is_413_with_tiny_body(small_app):
    app, _runtime = small_app
    sid = _session(app)
    result = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/chunks",
            client=LOCAL,
            headers=_local_headers(content_type="application/octet-stream", **{"x-chunk-index": "0"}),
            body=b"\x01\x00",
            add_content_length=False,
            content_length="65",
        )
    )
    assert result["status"] == 413, result


def test_invalid_content_length_is_400(small_app):
    app, _runtime = small_app
    sid = _session(app)
    result = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/chunks",
            client=LOCAL,
            headers=_local_headers(content_type="application/octet-stream", **{"x-chunk-index": "0"}),
            body=b"\x01\x00",
            add_content_length=False,
            content_length="not-a-number",
        )
    )
    assert result["status"] == 400, result
    assert result["body"]["error"] == "invalid_request"


def test_empty_chunk_is_400(small_app):
    app, _runtime = small_app
    sid = _session(app)
    result = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/chunks",
            client=LOCAL,
            headers=_local_headers(content_type="application/octet-stream", **{"x-chunk-index": "0"}),
            body=b"",
        )
    )
    assert result["status"] == 400, result
    assert "空" in result["body"]["message"]


def test_odd_pcm_under_limit_is_400(small_app):
    app, _runtime = small_app
    sid = _session(app)
    result = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/chunks",
            client=LOCAL,
            headers=_local_headers(content_type="application/octet-stream", **{"x-chunk-index": "0"}),
            body=b"\x01\x00\x02",
        )
    )
    assert result["status"] == 400, result
    assert "偶数" in result["body"]["message"]


def test_stt_without_content_length_oversize_is_413(small_app):
    app, _runtime = small_app
    body, ctype = multipart([("audio", b"x" * 300, "a.bin")])
    result = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path="/api/v1/stt",
            client=LOCAL,
            headers=_local_headers(content_type=ctype),
            body=body,
            add_content_length=False,
            chunked=True,
        )
    )
    assert result["status"] == 413, result
    assert result["body"]["error"] == "invalid_request"


def test_stt_multi_file_is_400(app):
    body, ctype = multipart([
        ("audio", b"hello-audio", "a.bin"),
        ("extra", b"second", "b.bin"),
    ])
    result = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path="/api/v1/stt",
            client=LOCAL,
            headers=_local_headers(content_type=ctype),
            body=body,
        )
    )
    assert result["status"] == 400, result
    assert "单个" in result["body"]["message"]


def test_upload_tmp_is_under_sandbox_not_system_tmp(small_app):
    _app, runtime = small_app
    assert runtime.upload_tmp is not None
    text = str(runtime.upload_tmp.resolve())
    assert runtime.upload_tmp.exists()
    assert text.endswith("/upload-tmp")
    assert Path("/tmp") not in runtime.upload_tmp.resolve().parents
    assert runtime.upload_tmp.resolve() != Path("/tmp")
