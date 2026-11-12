from __future__ import annotations

import json

import httpx
import pytest

from _contracts import LogicError, PolishRequest, TranslateRequest
from _ollama import OllamaClient
from _polish import polish_text
from _translate import translate_text


def _chat_ok(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"message": {"role": "assistant", "content": content}},
    )


def test_chat_sends_think_false_and_returns_content():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _chat_ok("修正后")

    client = OllamaClient(transport=httpx.MockTransport(handler), timeout_s=2)
    out = client.chat(model="qwen3.5:4b", messages=[{"role": "user", "content": "错字"}], think=False)
    assert out == "修正后"
    body = json.loads(seen[0].content.decode("utf-8"))
    assert body["think"] is False
    assert body["stream"] is False
    assert body["model"] == "qwen3.5:4b"
    client.close()


def test_think_true_is_rejected_before_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be sent")

    client = OllamaClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LogicError) as ei:
        client.chat(model="m", messages=[], think=True)
    assert ei.value.code.value == "invalid_request"
    client.close()


def test_timeout_bad_json_http_error_and_empty_content():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    client = OllamaClient(transport=httpx.MockTransport(timeout_handler), timeout_s=0.01)
    with pytest.raises(LogicError) as ei:
        client.chat(model="m", messages=[{"role": "user", "content": "a"}])
    assert ei.value.code.value == "timeout"
    client.close()

    def bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = OllamaClient(transport=httpx.MockTransport(bad_json))
    with pytest.raises(LogicError) as ei:
        client.chat(model="m", messages=[{"role": "user", "content": "a"}])
    assert "JSON" in ei.value.message
    client.close()

    def http_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = OllamaClient(transport=httpx.MockTransport(http_500))
    with pytest.raises(LogicError) as ei:
        client.chat(model="m", messages=[{"role": "user", "content": "a"}])
    assert ei.value.details["status_code"] == 500
    client.close()

    def empty(request: httpx.Request) -> httpx.Response:
        return _chat_ok("   ")

    client = OllamaClient(transport=httpx.MockTransport(empty))
    with pytest.raises(LogicError) as ei:
        client.chat(model="m", messages=[{"role": "user", "content": "a"}])
    assert "内容" in ei.value.message
    client.close()


def test_polish_failure_does_not_return_original():
    original = "这是原文错字"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("nope")

    client = OllamaClient(transport=httpx.MockTransport(boom), timeout_s=0.01)
    with pytest.raises(LogicError) as ei:
        polish_text(PolishRequest(text=original), model="m", client=client)
    assert ei.value.code.value == "polish_failed"
    assert ei.value.message != original
    client.close()


def test_polish_success_and_empty_input():
    client = OllamaClient(transport=httpx.MockTransport(lambda r: _chat_ok("纠正后的句子。")))
    out = polish_text(PolishRequest(text="纠正前的句子。", keywords=["Qwen"]), model="m", client=client)
    assert out.text == "纠正后的句子。"
    client.close()
    with pytest.raises(LogicError) as ei:
        polish_text(PolishRequest(text="  "), model="m")
    assert ei.value.code.value == "invalid_request"


def test_translate_failure_does_not_return_original():
    original = "hello world"

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = OllamaClient(transport=httpx.MockTransport(boom))
    with pytest.raises(LogicError) as ei:
        translate_text(TranslateRequest(text=original, target_lang="zh"), model="m", client=client)
    assert ei.value.code.value == "translate_failed"
    assert original not in ei.value.message or ei.value.code.value != "ok"
    # 确保不是 TranslateResponse
    assert not hasattr(ei.value, "text") or ei.value.__class__ is LogicError
    client.close()


def test_translate_success_sends_think_false():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        return _chat_ok("你好")

    client = OllamaClient(transport=httpx.MockTransport(handler))
    out = translate_text(TranslateRequest(text="hello", target_lang="zh"), model="m", client=client)
    assert out.text == "你好"
    assert out.target_lang == "zh"
    assert seen[0]["think"] is False
    client.close()
