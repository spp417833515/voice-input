from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from _contracts import LogicError
from _ocr import image_to_text


def test_empty_image_rejected():
    with pytest.raises(LogicError) as ei:
        image_to_text(b"", get_languages=lambda: ["eng"], image_to_string=lambda *a, **k: "x")
    assert ei.value.code.value == "invalid_request"


def test_missing_language_pack_is_explicit():
    with pytest.raises(LogicError) as ei:
        image_to_text(
            b"fakepng",
            lang="eng+jpn",
            get_languages=lambda: ["eng", "chi_sim", "osd"],
            image_to_string=lambda *a, **k: "should not run",
        )
    assert ei.value.code.value == "ocr_language_missing"
    assert "jpn" in ei.value.message
    assert "不会自动安装" in ei.value.message


def test_success_and_empty_result_with_mocks():
    out = image_to_text(
        _tiny_png(),
        lang="eng",
        get_languages=lambda: ["eng"],
        image_to_string=lambda img, lang: "HELLO",
    )
    assert out == "HELLO"
    with pytest.raises(LogicError) as ei:
        image_to_text(
            _tiny_png(),
            lang="eng",
            get_languages=lambda: ["eng"],
            image_to_string=lambda img, lang: "   ",
        )
    assert ei.value.code.value == "ocr_failed"


def test_tesseract_error_maps_language_missing():
    def boom(img, lang):
        raise RuntimeError("Failed loading language 'jpn'")

    with pytest.raises(LogicError) as ei:
        image_to_text(
            _tiny_png(),
            lang="eng",
            get_languages=lambda: ["eng"],
            image_to_string=boom,
        )
    assert ei.value.code.value == "ocr_language_missing"


def test_live_tesseract_synthetic_hello_if_eng_present():
    """真实 tesseract + 合成图，不是私人截图。"""
    pytesseract = pytest.importorskip("pytesseract")
    try:
        langs = set(pytesseract.get_languages(config=""))
    except Exception as exc:
        pytest.skip(f"无法查询 tesseract 语言包: {exc}")
    if "eng" not in langs:
        pytest.skip("本机未安装 tesseract eng 语言包")
    text = image_to_text(_hello_image(), lang="eng")
    assert "HELLO" in text.replace(" ", "").upper()


def _tiny_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def _hello_image() -> bytes:
    img = Image.new("RGB", (240, 80), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
    except OSError:
        font = ImageFont.load_default()
    draw.text((10, 20), "HELLO", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
