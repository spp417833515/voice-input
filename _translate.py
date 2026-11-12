"""本地 Ollama 翻译。失败必须抛错，禁止把原文当作译文成功返回。"""
from __future__ import annotations

from _contracts import ErrorCode, LogicError, TranslateRequest, TranslateResponse
from _ollama import DEFAULT_OLLAMA_BASE_URL, OllamaClient

_SYSTEM = (
    "你是翻译器。只把用户文本翻译成指定目标语言。"
    "不要解释，不要附加原文，不要把失败时的原文当成译文输出。"
)


def translate_text(
    request: TranslateRequest,
    *,
    model: str,
    client: OllamaClient | None = None,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
) -> TranslateResponse:
    if not request.text.strip():
        raise LogicError(ErrorCode.invalid_request, "翻译文本不能为空")
    if not request.target_lang.strip():
        raise LogicError(ErrorCode.invalid_request, "target_lang 不能为空")
    user = f"目标语言: {request.target_lang}\n\n{request.text}"
    owns = client is None
    ol = client or OllamaClient(base_url=base_url)
    try:
        try:
            out = ol.chat(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user},
                ],
                think=False,
            )
        except LogicError as exc:
            raise LogicError(ErrorCode.translate_failed, exc.message, exc.details) from exc
    finally:
        if owns:
            ol.close()
    if not out.strip():
        raise LogicError(ErrorCode.translate_failed, "译文为空，不得把原文当作译文")
    return TranslateResponse(text=out, target_lang=request.target_lang)
