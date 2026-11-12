"""Ollama HTTP 客户端（/api/chat，think:false）。供 _polish 与 _translate 复用，避免两套实现。

真实复用理由：纠错和翻译都要走同一官方 chat 接口、同一超时/JSON/失败可见处理，
以及强制 think=false；若各写一份，失败路径与请求体必然分叉。
本模块不含业务提示词，只负责协议。
"""
from __future__ import annotations

from typing import Any

import httpx

from _contracts import ErrorCode, LogicError

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_S = 120.0


class OllamaClient:
    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        think: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """调用 /api/chat。默认且产品要求 think=false。

        失败（超时、非 2xx、非法 JSON、空内容）必须抛 LogicError，
        调用方不得改把原文当成成功返回。
        """
        if think:
            raise LogicError(
                ErrorCode.invalid_request,
                "Ollama chat 必须使用 think=false，禁止开启 think",
            )
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
        }
        if extra:
            payload.update(extra)
            payload["think"] = False
            payload["stream"] = False
        url = "/api/chat"
        try:
            resp = self._client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise LogicError(ErrorCode.timeout, f"Ollama 请求超时: {exc}") from exc
        except httpx.RequestError as exc:
            raise LogicError(
                ErrorCode.dependency_failed, f"无法连接 Ollama: {exc}"
            ) from exc
        if resp.status_code >= 400:
            snippet = (resp.text or "")[:300]
            raise LogicError(
                ErrorCode.dependency_failed,
                f"Ollama 返回 HTTP {resp.status_code}",
                details={"status_code": resp.status_code, "body": snippet},
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise LogicError(
                ErrorCode.dependency_failed, f"Ollama 响应不是合法 JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise LogicError(ErrorCode.dependency_failed, "Ollama 响应根节点必须是对象")
        message = data.get("message")
        if not isinstance(message, dict):
            raise LogicError(ErrorCode.dependency_failed, "Ollama 响应缺少 message 对象")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LogicError(ErrorCode.dependency_failed, "Ollama 响应缺少文本内容")
        return content.strip()
