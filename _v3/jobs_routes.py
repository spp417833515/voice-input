"""job 查询 / 本机 claim / ack。投递状态只以 jobs 为准。"""
from __future__ import annotations

import time

from fastapi import APIRouter

from _contracts import (
    JobAckAccepted,
    JobAckRequest,
    JobClaimRequest,
    JobClaimed,
    JobQueryFound,
    JobQueryUnknown,
)
from _v3.runtime import Runtime

# 长轮询：在 timeout_s（上限 25s）内每 0.2s 再扫一次，期间无任务返回 null。不重投、不编造 job。
CLAIM_WAIT_MAX_S = 25.0
CLAIM_POLL_S = 0.2


def build_jobs_router(runtime: Runtime) -> APIRouter:
    router = APIRouter(tags=["jobs"])
    @router.post("/api/v1/jobs/claim", response_model=JobClaimed | None)
    def claim(body: JobClaimRequest | None = None) -> JobClaimed | None:
        wait = min(body.timeout_s or 0.0, CLAIM_WAIT_MAX_S) if body is not None else 0.0
        deadline = time.monotonic() + wait
        while True:
            claimed = runtime.jobs.claim(wait_s=wait)
            if claimed is not None or time.monotonic() >= deadline:
                return claimed
            time.sleep(CLAIM_POLL_S)

    @router.post("/api/v1/jobs/{job_id}/ack", response_model=JobAckAccepted)
    def ack(job_id: str, body: JobAckRequest) -> JobAckAccepted:
        record = runtime.jobs.ack(job_id, body)
        return JobAckAccepted(job_id=record.job_id, state=record.state)

    @router.get("/api/v1/jobs/{job_id}", response_model=JobQueryFound | JobQueryUnknown)
    def query(job_id: str) -> JobQueryFound | JobQueryUnknown:
        return runtime.jobs.get(job_id)

    return router
