---
brief: Tesseract OCR + 三级翻译降级 HTTP 端点 — app.py:265 _translate_text + app.py:306 /api/ocr_translate
tags: [后端, tesseract, ocr, translate, deep-translator]
logic_flow: |
  flowchart TD
    Start([POST /api/ocr_translate<br/>image + target_lang]) --> OCR["pytesseract.image_to_string<br/>lang='eng+chi_sim+jpn'<br/>app.py:319-322"]
    OCR --> Empty{"raw_text 空?"}
    Empty -->|是| Err502_OCR([HTTP 502 没有识别到文字])
    Empty -->|否| Raw{"target_lang=='raw'?"}
    Raw -->|是| RetRaw([返回原文 model='tesseract'])
    Raw -->|否| Trans["_translate_text(raw, target)<br/>app.py:265"]
    Trans --> G["1. GoogleTranslator<br/>app.py:271-276"]
    G -->|失败| B["2. BaiduTranslator (需 appid/appkey)<br/>app.py:279-291"]
    B -->|失败| MM["3. MyMemoryTranslator<br/>app.py:293-301"]
    G --> Ret([返回 translated + engine])
    B --> Ret
    MM --> Ret
    MM -->|全失败| Err502_T([HTTP 502 翻译失败])
---

# 截图OCR翻译

## 一句话定位

接收图片，用 tesseract 多语言识别文字（英+中简+日），按 Google→百度→MyMemory 降级翻译到 target_lang。

## 关联代码路径

- `app.py:265-303` `_translate_text(text, target) -> (translated, engine)` — 三级降级链
- `app.py:306-348` `@app.post("/api/ocr_translate")` — FastAPI 端点
- `app.py:37` `TRANSLATE_TARGET_LANG = "zh"` — 默认目标语言
- `app.py:38-39` `BAIDU_APPID / BAIDU_APPKEY` — 百度 API 凭证
- 调用方：`daemon.py:1572-1578` `_do_screenshot_translate` 在截图后 httpx.post

## 功能节点说明

- OCR 部分：`pytesseract.image_to_string(img, lang="eng+chi_sim+jpn")`，缺少 tesseract 时返回 502 并提示 `sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim`。
- target_lang=="raw" 短路：直接返回 OCR 原文，不进入翻译链。
- `lang_map`：`{zh→zh-CN, en→en, ja→ja, ko→ko}`（app.py:337）；百度子映射 `{zh-CN→zh, ja→jp, ko→kor}`（app.py:283）；MyMemory 子映射 `{zh-CN→zh-CN, en→en-GB, ja→ja-JP, ko→ko-KR}`（app.py:296）。
- 端点契约：返回 `{text, model}`，model 形如 `"tesseract + GoogleTranslate"`，daemon 端只取 `text`。

## 关联复用

- 无 L5 复用单元被本模块独占调用。
