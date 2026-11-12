"""Tesseract OCR 能力封装。缺语言包必须显式失败，不在本层安装或下载语言包。"""
from __future__ import annotations

import io
from collections.abc import Callable

from _contracts import ErrorCode, LogicError

DEFAULT_OCR_LANGS = "eng+chi_sim"


def _missing_language(message: str) -> bool:
    lower = message.lower()
    return (
        "failed loading language" in lower
        or "error opening data file" in lower
        or "tessdata" in lower and "not find" in lower
        or "please make sure the tessdata" in lower
    )


def image_to_text(
    image_bytes: bytes,
    *,
    lang: str = DEFAULT_OCR_LANGS,
    image_to_string: Callable[..., str] | None = None,
    get_languages: Callable[[], list[str]] | None = None,
) -> str:
    """从图片字节识别文本。默认 eng+chi_sim，不默认也不安装日文包。"""
    if not image_bytes:
        raise LogicError(ErrorCode.invalid_request, "OCR 图像不能为空")
    if image_to_string is None or get_languages is None:
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            raise LogicError(
                ErrorCode.ocr_failed, f"OCR 依赖不可用: {exc}"
            ) from exc
        if get_languages is None:
            def get_languages() -> list[str]:
                try:
                    langs = pytesseract.get_languages(config="")
                    return list(langs)
                except Exception as exc:
                    raise LogicError(
                        ErrorCode.ocr_failed, f"无法查询 Tesseract 语言包: {exc}"
                    ) from exc
        if image_to_string is None:
            def image_to_string(img, lang: str) -> str:
                return pytesseract.image_to_string(img, lang=lang)

        image_factory = Image
    else:
        from PIL import Image as image_factory  # type: ignore

    requested = [part for part in lang.split("+") if part and part != "osd"]
    try:
        available = set(get_languages())
    except LogicError:
        raise
    missing = [name for name in requested if name not in available]
    if missing:
        raise LogicError(
            ErrorCode.ocr_language_missing,
            "缺少 Tesseract 语言包: " + ", ".join(missing) + "。本层不会自动安装（含日文包）。",
            details={"missing": missing, "available": sorted(available)},
        )
    try:
        img = image_factory.open(io.BytesIO(image_bytes))
        text = image_to_string(img, lang=lang)
    except LogicError:
        raise
    except Exception as exc:
        msg = str(exc)
        if _missing_language(msg):
            raise LogicError(
                ErrorCode.ocr_language_missing,
                f"Tesseract 语言包不可用: {msg}",
            ) from exc
        raise LogicError(ErrorCode.ocr_failed, f"OCR 失败: {exc}") from exc
    cleaned = (text or "").strip()
    if not cleaned:
        raise LogicError(ErrorCode.ocr_failed, "没有识别到文字")
    return cleaned
