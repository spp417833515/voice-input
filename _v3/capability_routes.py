"""能力型 API：只转写 / 润色 / 翻译 / OCR+翻译。产品流不得用这些接口入队。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile

from _contracts import (
    ErrorCode,
    LogicError,
    OcrTranslateResponse,
    PolishRequest,
    PolishResponse,
    SttResponse,
    TranslateRequest,
    TranslateResponse,
)
from _v3.limits import AUDIO_UPLOAD_NOTE, IMAGE_UPLOAD_NOTE, read_single_form_file
from _v3.runtime import Runtime


def _reject_silence(audio: bytes) -> None:
    if not audio:
        raise LogicError(ErrorCode.invalid_request, "音频不能为空")
    if len(audio) >= 2 and not any(audio):
        raise LogicError(ErrorCode.invalid_request, "音频为静音，无法转写")


def build_capability_router(runtime: Runtime) -> APIRouter:
    router = APIRouter(tags=["capabilities"])
    @router.post("/api/v1/stt", response_model=SttResponse, description=AUDIO_UPLOAD_NOTE)
    async def stt(
        request: Request,
        language: str | None = Form(default=None),
        context: str | None = Form(default=None),
        audio: UploadFile | None = File(default=None),
    ) -> SttResponse:
        del audio
        payload = await read_single_form_file(
            request,
            field="audio",
            limit=runtime.max_upload_bytes,
            missing="必须提供音频文件",
        )
        _reject_silence(payload)
        return runtime.utterance.transcribe_public(payload, language=language, context=context)

    @router.post("/api/v1/polish", response_model=PolishResponse)
    def polish(body: PolishRequest) -> PolishResponse:
        if runtime.polish is None:
            raise LogicError(ErrorCode.polish_failed, "润色能力不可用")
        return runtime.polish(body)

    @router.post("/api/v1/translate", response_model=TranslateResponse)
    def translate(body: TranslateRequest) -> TranslateResponse:
        if runtime.translate is None:
            raise LogicError(ErrorCode.translate_failed, "翻译能力不可用")
        return runtime.translate(body)

    @router.post("/api/v1/ocr-translate", response_model=OcrTranslateResponse, description=IMAGE_UPLOAD_NOTE)
    async def ocr_translate(
        request: Request,
        target_lang: str = Form(...),
        lang: str | None = Form(default=None),
        image: UploadFile | None = File(default=None),
    ) -> OcrTranslateResponse:
        del image
        if runtime.ocr is None:
            raise LogicError(ErrorCode.ocr_failed, "OCR 能力不可用")
        payload = await read_single_form_file(
            request,
            field="image",
            limit=runtime.max_upload_bytes,
            missing="必须提供图片",
        )
        ocr_kwargs: dict = {}
        if lang:
            ocr_kwargs["lang"] = lang
        ocr_text = runtime.ocr(payload, **ocr_kwargs)
        if not (ocr_text or "").strip():
            raise LogicError(ErrorCode.ocr_failed, "OCR 未识别到文字")
        if target_lang.strip().lower() == "raw":
            return OcrTranslateResponse(text=ocr_text, ocr_text=ocr_text, target_lang="raw")
        if runtime.translate is None:
            raise LogicError(ErrorCode.translate_failed, "翻译能力不可用")
        translated = runtime.translate(TranslateRequest(text=ocr_text, target_lang=target_lang))
        if translated.text == ocr_text and target_lang.strip().lower() not in {"raw", "zh", "chi", "chi_sim"}:
            # 不在这里用相等性判断失败；翻译层已禁止把原文当成功。保持结果。
            pass
        return OcrTranslateResponse(text=translated.text, ocr_text=ocr_text, target_lang=target_lang)

    return router
