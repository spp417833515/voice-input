"""只纠错（typo-only）调用 Ollama。失败必须抛错，禁止把原文当作成功返回。"""
from __future__ import annotations

from _contracts import ErrorCode, LogicError, PolishRequest, PolishResponse
from _ollama import DEFAULT_OLLAMA_BASE_URL, OllamaClient

_SYSTEM = (
    "你只修正明显的语音识别错字、标点和必要空格。"
    "保持原意和原句结构，不要润色、不要改写、不要增删事实。"
    "只输出纠正后的文本，不要解释。"
)


def polish_text(
    request: PolishRequest,
    *,
    model: str,
    client: OllamaClient | None = None,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
) -> PolishResponse:
    text = request.text
    if not text.strip():
        raise LogicError(ErrorCode.invalid_request, "纠错文本不能为空")
    hints = ""
    if request.keywords:
        hints = "专有名词或关键词（不得改成别的词）：" + "、".join(request.keywords)
    user = text if not hints else f"{hints}\n\n{text}"
    if request.language:
        user = f"语言: {request.language}\n{user}"
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
            raise LogicError(ErrorCode.polish_failed, exc.message, exc.details) from exc
    finally:
        if owns:
            ol.close()
    if not out.strip():
        raise LogicError(ErrorCode.polish_failed, "纠错结果为空，不得改回原文充成功")
    return PolishResponse(text=out)
