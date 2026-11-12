"""产品 utterance 路由：只调用唯一业务服务，不在此重写幂等或 fail-closed。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile

from _contracts import (
    ErrorCode,
    JobRecord,
    JobState,
    LogicError,
    UtteranceAccepted,
    UtteranceCreate,
    UtteranceFromAudio,
    UtteranceResendNeedsConfirmation,
    UtteranceResendQueued,
    UtteranceResendRequest,
    UtteranceSource,
)
from _v3.limits import AUDIO_UPLOAD_NOTE, read_single_form_file
from _v3.runtime import Runtime


def _accepted(record: JobRecord) -> UtteranceAccepted:
    if record.state != JobState.queued:
        raise LogicError(
            ErrorCode.internal,
            "业务未入队，不能用 queued 成功体返回",
            details={"job_id": record.job_id, "state": record.state},
        )
    return UtteranceAccepted(job_id=record.job_id, state=JobState.queued, text=record.text)


def build_utterance_router(runtime: Runtime) -> APIRouter:
    router = APIRouter(tags=["utterances"])
    @router.post("/api/v1/utterances", response_model=UtteranceAccepted)
    def create_utterance(body: UtteranceCreate) -> UtteranceAccepted:
        if body.source != UtteranceSource.desktop:
            raise LogicError(ErrorCode.invalid_request, "桌面 utterance 的 source 必须是 desktop")
        return _accepted(runtime.utterance.submit_session_final(body))

    @router.post("/api/v1/utterances/from-audio", response_model=UtteranceAccepted, description=AUDIO_UPLOAD_NOTE)
    async def from_audio(
        request: Request,
        job_id: str | None = Form(default=None),
        language: str | None = Form(default=None),
        context: str | None = Form(default=None),
        focus_id: str = Form(default=""),
        audio: UploadFile | None = File(default=None),
    ) -> UtteranceAccepted:
        del audio
        if not job_id or not job_id.strip():
            raise LogicError(ErrorCode.invalid_request, "job_id 必传，服务器不会代为生成")
        payload = await read_single_form_file(
            request,
            field="audio",
            limit=runtime.max_upload_bytes,
            missing="必须提供音频文件",
        )
        body = UtteranceFromAudio(job_id=job_id.strip(), language=language, context=context)
        return _accepted(runtime.utterance.submit_from_audio(body, payload, focus_id=focus_id))

    @router.post(
        "/api/v1/utterances/resend",
        response_model=UtteranceResendQueued | UtteranceResendNeedsConfirmation,
    )
    def resend(body: UtteranceResendRequest) -> UtteranceResendQueued | UtteranceResendNeedsConfirmation:
        return runtime.utterance.submit_resend(body)

    return router
