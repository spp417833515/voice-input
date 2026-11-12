"""本机产品 API 客户端（守护进程用）。只访问回环 https，校验项目证书，禁止 verify=False。"""
from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import httpx

from _contracts import SIDECAR_CHUNK_HEADER
from _jobs_client import DEFAULT_BASE_URL, DEFAULT_CERT, JobsClient


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _check(resp: httpx.Response) -> Any:
    if resp.status_code < 400:
        return resp.json() if resp.content else None
    try:
        body = resp.json()
        code, message = str(body.get("error", "internal")), str(body.get("message", resp.text[:200]))
    except Exception:
        code, message = "internal", resp.text[:200]
    raise ApiError(resp.status_code, code, message)


class DesktopApi:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        cert: Path | None = DEFAULT_CERT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not (base_url.startswith("https://127.0.0.1") or base_url.startswith("https://localhost")):
            raise ValueError("DesktopApi 只允许本机 https")
        kwargs: dict[str, Any] = {
            "base_url": base_url,
            "timeout": httpx.Timeout(10.0, read=130.0),
        }
        if transport is not None:
            kwargs["transport"] = transport
        else:
            if cert is None or not Path(cert).is_file():
                raise ValueError("缺少项目证书，拒绝关闭 TLS 校验")
            kwargs["verify"] = ssl.create_default_context(cafile=str(cert))
        self._client = httpx.Client(**kwargs)
        self.jobs = JobsClient(base_url, client=self._client)

    def close(self) -> None:
        self._client.close()

    # ---- 识别会话 -------------------------------------------------------
    def create_session(self, *, language: str | None = None) -> str:
        body = _check(self._client.post("/api/v1/recognize/sessions", json={"language": language}))
        return str(body["session_id"])

    def upload_chunk(self, session_id: str, index: int, pcm: bytes) -> None:
        _check(
            self._client.post(
                f"/api/v1/recognize/sessions/{session_id}/chunks",
                content=pcm,
                headers={SIDECAR_CHUNK_HEADER: str(index), "content-type": "application/octet-stream"},
            )
        )

    def finish_session(self, session_id: str) -> dict:
        return _check(self._client.post(f"/api/v1/recognize/sessions/{session_id}/finish"))

    def cancel_session(self, session_id: str) -> dict:
        return _check(self._client.post(f"/api/v1/recognize/sessions/{session_id}/cancel"))

    # ---- utterance ------------------------------------------------------
    def submit_utterance(self, *, job_id: str, session_id: str, focus_id: str) -> dict:
        return _check(
            self._client.post(
                "/api/v1/utterances",
                json={"job_id": job_id, "session_id": session_id, "source": "desktop", "focus_id": focus_id},
            )
        )

    def resend(self, *, job_id: str, text: str, original_job_id: str, focus_id: str, confirm: bool) -> dict:
        return _check(
            self._client.post(
                "/api/v1/utterances/resend",
                json={
                    "job_id": job_id,
                    "text": text,
                    "original_job_id": original_job_id,
                    "focus_id": focus_id,
                    "confirm": confirm,
                },
            )
        )

    # ---- 平台状态 -------------------------------------------------------
    def post_platform_event(self, event: dict) -> None:
        _check(self._client.post("/api/v1/events/platform", json=event))

    def get_config(self) -> dict:
        return _check(self._client.get("/api/v1/config"))
