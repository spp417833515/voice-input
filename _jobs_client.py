"""本机 jobs HTTP 客户端。只 claim/ack/查询，不重做业务决策。

默认校验项目证书，禁止 verify=False。只访问回环。
"""
from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import httpx

from _inject import AckResult, ClaimAckLog

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CERT = PROJECT_ROOT / "certs" / "cert.pem"
DEFAULT_BASE_URL = "https://127.0.0.1:17945"


class JobsClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        cert: Path | None = DEFAULT_CERT,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        acks: ClaimAckLog | None = None,
    ) -> None:
        allowed = base_url.startswith("https://127.0.0.1") or base_url.startswith("https://localhost") or base_url.startswith("http://127.0.0.1")
        if not allowed:
            raise ValueError("JobsClient 只允许回环地址")
        kwargs: dict = {"base_url": base_url, "timeout": 30.0}
        if transport is not None:
            kwargs["transport"] = transport
        elif client is None:
            if cert is None or not Path(cert).is_file():
                raise ValueError("缺少项目证书，拒绝关闭 TLS 校验")
            kwargs["verify"] = ssl.create_default_context(cafile=str(cert))
        self._owns = client is None
        self._client = client or httpx.Client(**kwargs)
        self.acks = acks or ClaimAckLog()

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def __enter__(self) -> "JobsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def claim(self, timeout_s: float | None = None) -> dict[str, Any] | None:
        payload: dict[str, Any] = {}
        if timeout_s is not None:
            payload["timeout_s"] = timeout_s
        resp = self._client.post("/api/v1/jobs/claim", json=payload or None)
        resp.raise_for_status()
        data = resp.json()
        return data

    def get_job(self, job_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/api/v1/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    def ack(self, job_id: str, claim_id: str, result: AckResult, *, detail: str | None = None) -> dict[str, Any]:
        self.acks.record(claim_id, result)
        payload: dict[str, Any] = {"claim_id": claim_id, "result": result}
        if detail:
            payload["detail"] = detail
        resp = self._client.post(f"/api/v1/jobs/{job_id}/ack", json=payload)
        resp.raise_for_status()
        return resp.json()
