from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import asyncio
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MODEL_CACHE_DIR = BASE_DIR / ".cache" / "models"
CONFIG_PATH = BASE_DIR / ".config.json"
HISTORY_PATH = BASE_DIR / ".cache" / "history.jsonl"

WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "default")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "zh")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "120"))
WHISPER_VAD_FILTER = os.getenv("WHISPER_VAD_FILTER", "true").lower() == "true"
CUSTOM_KEYWORDS: list[str] = []
TRANSLATE_TARGET_LANG = "zh"
BAIDU_APPID = ""
BAIDU_APPKEY = ""
HOTKEY_VOICE = "ctrl+grave"
HOTKEY_SCREENSHOT = "alt+x"
HOTKEY_REPEAT = "ctrl+shift+z"


def _load_config() -> None:
    """从 .config.json 加载持久化配置。"""
    global OLLAMA_MODEL, CUSTOM_KEYWORDS, WHISPER_MODEL_NAME, TRANSLATE_TARGET_LANG
    global HOTKEY_VOICE, HOTKEY_SCREENSHOT, HOTKEY_REPEAT
    global BAIDU_APPID, BAIDU_APPKEY
    if not CONFIG_PATH.exists():
        return
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        OLLAMA_MODEL = cfg.get("ollama_model", OLLAMA_MODEL)
        CUSTOM_KEYWORDS = cfg.get("custom_keywords", CUSTOM_KEYWORDS)
        WHISPER_MODEL_NAME = cfg.get("whisper_model", WHISPER_MODEL_NAME)
        TRANSLATE_TARGET_LANG = cfg.get("translate_target_lang", TRANSLATE_TARGET_LANG)
        BAIDU_APPID = cfg.get("baidu_appid", BAIDU_APPID)
        BAIDU_APPKEY = cfg.get("baidu_appkey", BAIDU_APPKEY)
        HOTKEY_VOICE = cfg.get("hotkey_voice", HOTKEY_VOICE)
        HOTKEY_SCREENSHOT = cfg.get("hotkey_screenshot", HOTKEY_SCREENSHOT)
        HOTKEY_REPEAT = cfg.get("hotkey_repeat", HOTKEY_REPEAT)
    except Exception:
        pass


def _save_config() -> None:
    """将当前模型配置写入 .config.json（合并已有字段）。"""
    cfg = {
        "ollama_model": OLLAMA_MODEL,
        "custom_keywords": CUSTOM_KEYWORDS,
        "whisper_model": WHISPER_MODEL_NAME,
        "translate_target_lang": TRANSLATE_TARGET_LANG,
        "baidu_appid": BAIDU_APPID,
        "baidu_appkey": BAIDU_APPKEY,
        "hotkey_voice": HOTKEY_VOICE,
        "hotkey_screenshot": HOTKEY_SCREENSHOT,
        "hotkey_repeat": HOTKEY_REPEAT,
    }
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text())
            existing.update(cfg)
            cfg = existing
        except Exception:
            pass
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    except Exception:
        pass


_load_config()

app = FastAPI(title="Ollama Voice Input")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _save_history(raw_text: str, final_text: str, mode: str, language: str) -> None:
    """追加一条转写记录到 history.jsonl。"""
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "raw_text": raw_text,
        "final_text": final_text,
        "mode": mode,
        "language": language,
    }
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


_whisper_model = None
_whisper_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------


def get_whisper_model():
    global _whisper_model

    if _whisper_model is not None:
        return _whisper_model

    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model

        from faster_whisper import WhisperModel

        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _whisper_model = WhisperModel(
            WHISPER_MODEL_NAME,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
            download_root=str(MODEL_CACHE_DIR),
        )
        return _whisper_model


def transcribe_audio(audio_path: str, language: str) -> str:
    model = get_whisper_model()
    # 将自定义关键词作为 initial_prompt 传入，提升专有名词识别率
    prompt = "，".join(CUSTOM_KEYWORDS) if CUSTOM_KEYWORDS else None
    # 短录音（<3秒）关闭 VAD 过滤，避免误判为静音丢弃
    import soundfile as _sf
    _audio_info = _sf.info(audio_path)
    use_vad = WHISPER_VAD_FILTER and _audio_info.duration >= 3.0
    segments, _info = model.transcribe(
        audio_path,
        language=None if language == "auto" else language,
        vad_filter=use_vad,
        beam_size=5,
        initial_prompt=prompt,
    )
    # 过滤 Whisper 幻觉（短音频/静音容易产生虚假文本）
    # 策略: 段落声称的时间远超实际音频时长 → 幻觉
    audio_duration = _audio_info.duration
    real_segments = []
    for seg in segments:
        # 幻觉特征: 0.3s 音频却生成 [0.00-29.98] 的段落
        if seg.end > audio_duration * 2 + 1.0:
            continue
        # 辅助过滤: 高 no_speech_prob（无 initial_prompt 干扰时生效）
        if seg.no_speech_prob > 0.6:
            continue
        real_segments.append(seg.text.strip())
    text = " ".join(real_segments).strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail="Whisper 没有识别到语音内容。可能原因：录音音量太低、环境噪音、或录音时间太短。",
        )
    return text


# ---------------------------------------------------------------------------
# Ollama 调用
# ---------------------------------------------------------------------------


def build_prompt(mode: str, raw_text: str) -> list[dict[str, str]]:
    if mode == "raw":
        return []

    if mode == "translate_en":
        system = (
            "你是一个翻译助手。"
            "请把用户给出的中文语音转写结果翻译成自然、简洁的英文。"
        )
        if CUSTOM_KEYWORDS:
            kw_str = "、".join(CUSTOM_KEYWORDS)
            system += f"以下为专有名词，翻译时请保留或使用其正确英文形式：{kw_str}。"
        system += "只输出最终英文，不要解释。"
    else:
        raise HTTPException(status_code=400, detail=f"不支持的模式: {mode}")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": raw_text},
    ]


async def call_ollama(mode: str, raw_text: str) -> str:
    messages = build_prompt(mode, raw_text)
    if not messages:
        return raw_text

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": 0.1,
    }

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/v1/chat/completions", json=payload
            )
            response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ollama 调用失败: {exc}") from exc

    data = response.json()
    choices = data.get("choices", [])
    content = choices[0]["message"]["content"].strip() if choices else ""
    if not content:
        raise HTTPException(status_code=502, detail="Ollama 没有返回结果")
    return content


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "setup.html")


@app.get("/setup")
async def setup_page():
    return FileResponse(STATIC_DIR / "setup.html")


@app.get("/history")
async def history_page():
    return FileResponse(STATIC_DIR / "history.html")


# ---------------------------------------------------------------------------
# 业务 API
# ---------------------------------------------------------------------------


@app.get("/api/history")
async def get_history(limit: int = 50, offset: int = 0):
    """返回语音转写历史记录（最新在前）。"""
    if not HISTORY_PATH.exists():
        return {"records": [], "total": 0}
    try:
        lines = HISTORY_PATH.read_text(encoding="utf-8").strip().split("\n")
        lines = [l for l in lines if l.strip()]
    except Exception:
        return {"records": [], "total": 0}
    records = []
    for line in reversed(lines):
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    total = len(records)
    return {"records": records[offset : offset + limit], "total": total}


@app.post("/api/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    mode: str = Form("polish"),
    language: str = Form(WHISPER_LANGUAGE),
):
    suffix = Path(audio.filename or "recording.webm").suffix or ".webm"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        while True:
            chunk = await audio.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)

    try:
        raw_text = await asyncio.to_thread(transcribe_audio, tmp_path, language)
        _save_history(raw_text, raw_text, mode, language)
        return {"raw_text": raw_text, "final_text": raw_text, "mode": mode}
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


def _translate_text(text: str, target: str) -> tuple[str, str]:
    """翻译文本，降级链：Google → 百度 → MyMemory。返回 (翻译结果, 引擎名)。"""
    from deep_translator import GoogleTranslator, MyMemoryTranslator
    errors = []

    # 1. Google Translate（主引擎）
    try:
        result = GoogleTranslator(source="auto", target=target).translate(text)
        if result:
            return result, "GoogleTranslate"
    except Exception as exc:
        errors.append(f"Google: {exc}")

    # 2. 百度翻译（需要 appid + appkey）
    if BAIDU_APPID and BAIDU_APPKEY:
        try:
            from deep_translator import BaiduTranslator
            # 百度 API 语言代码映射
            baidu_lang = {"zh-CN": "zh", "en": "en", "ja": "jp", "ko": "kor"}.get(target, "zh")
            result = BaiduTranslator(
                appid=BAIDU_APPID, appkey=BAIDU_APPKEY,
                source="auto", target=baidu_lang,
            ).translate(text)
            if result:
                return result, "BaiduTranslate"
        except Exception as exc:
            errors.append(f"Baidu: {exc}")

    # 3. MyMemory（免费兜底，无需 API key）
    try:
        # MyMemory 需要完整语言代码
        mm_lang = {"zh-CN": "zh-CN", "en": "en-GB", "ja": "ja-JP", "ko": "ko-KR"}.get(target, "zh-CN")
        result = MyMemoryTranslator(source="autodetect", target=mm_lang).translate(text)
        if result:
            return result, "MyMemory"
    except Exception as exc:
        errors.append(f"MyMemory: {exc}")

    raise RuntimeError(f"所有翻译引擎均失败: {'; '.join(errors)}")


@app.post("/api/ocr_translate")
async def ocr_translate(
    image: UploadFile = File(...),
    target_lang: str = Form("zh"),
):
    """tesseract OCR 提取文字 + 多引擎翻译（Google→百度→MyMemory）。"""
    import pytesseract
    from PIL import Image
    import io

    content = await image.read()
    img = Image.open(io.BytesIO(content))

    # ---- 第 1 步：tesseract OCR 提取文字 ----
    try:
        # 自动检测多语言（英+中+日+韩）
        raw_text = pytesseract.image_to_string(img, lang="eng+chi_sim+jpn").strip()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"OCR 失败（请确认已安装 tesseract: sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim）: {exc}",
        ) from exc

    if not raw_text:
        raise HTTPException(status_code=502, detail="没有识别到文字")

    # 如果只要原文，直接返回
    if target_lang == "raw":
        return {"text": raw_text, "model": "tesseract"}

    # ---- 第 2 步：多引擎翻译（Google → 百度 → MyMemory） ----
    lang_map = {"zh": "zh-CN", "en": "en", "ja": "ja", "ko": "ko"}
    target = lang_map.get(target_lang, "zh-CN")

    try:
        translated, engine = _translate_text(raw_text, target)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"翻译失败: {exc}") from exc

    if not translated:
        raise HTTPException(status_code=502, detail="翻译没有返回结果")

    return {"text": translated, "model": f"tesseract + {engine}"}


# ---------------------------------------------------------------------------
# Setup API —— 环境自检 / 模型管理
# ---------------------------------------------------------------------------


@app.get("/api/setup/check")
async def setup_check():
    """一次性检查所有组件状态。"""
    # 扫描已下载的 whisper 模型
    downloaded_whisper = []
    if MODEL_CACHE_DIR.exists():
        for p in MODEL_CACHE_DIR.iterdir():
            if p.is_dir():
                name = p.name.lower()
                for tag in ("large-v3-turbo", "large-v3", "large-v2", "medium", "small", "base", "tiny"):
                    if tag in name:
                        downloaded_whisper.append(tag)
                        break

    result = {
        "ollama": {"installed": False, "running": False, "version": None},
        "ollama_models": [],
        "whisper": {
            "model": WHISPER_MODEL_NAME,
            "downloaded": WHISPER_MODEL_NAME in downloaded_whisper,
            "downloaded_models": downloaded_whisper,
        },
        "system_tools": {},
        "config": {
            "ollama_model": OLLAMA_MODEL,
            "whisper_model": WHISPER_MODEL_NAME,
            "whisper_device": WHISPER_DEVICE,
            "whisper_language": WHISPER_LANGUAGE,
            "voice_mode": "raw",
            "translate_target_lang": TRANSLATE_TARGET_LANG,
            "baidu_appid": BAIDU_APPID,
            "baidu_appkey": "***" if BAIDU_APPKEY else "",
            "custom_keywords": CUSTOM_KEYWORDS,
            "hotkey_voice": HOTKEY_VOICE,
            "hotkey_screenshot": HOTKEY_SCREENSHOT,
            "hotkey_repeat": HOTKEY_REPEAT,
        },
    }

    # 1. ollama 是否安装
    ollama_bin = shutil.which("ollama")
    if ollama_bin:
        result["ollama"]["installed"] = True
        try:
            ver = subprocess.run(
                ["ollama", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            result["ollama"]["version"] = ver.stdout.strip()
        except Exception:
            pass

    # 2. ollama 是否运行 + 列出已有模型
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            result["ollama"]["running"] = True
            result["ollama_models"] = [m["name"] for m in data.get("models", [])]
    except Exception:
        pass

    # 3. 系统工具检查
    for tool in ["maim", "xclip", "notify-send"]:
        result["system_tools"][tool] = shutil.which(tool) is not None

    return result


@app.post("/api/setup/pull_model")
async def pull_model(model: str = Form(...)):
    """拉取 ollama 模型，流式返回 SSE 进度。"""

    async def stream_progress():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_BASE_URL}/api/pull",
                    json={"name": model, "stream": True},
                ) as resp:
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if line:
                            yield f"data: {line}\n\n"
            yield 'data: {"status":"done"}\n\n'
        except Exception as exc:
            import json

            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/setup/delete_model")
async def delete_model(model: str = Form(...)):
    """删除已下载的 ollama 模型。"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                "DELETE",
                f"{OLLAMA_BASE_URL}/api/delete",
                json={"name": model},
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"模型 {model} 不存在")
            resp.raise_for_status()
            return {"ok": True, "model": model}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"删除模型失败: {exc}") from exc


@app.post("/api/setup/install_ollama")
async def install_ollama():
    """尝试安装 ollama（调用官方安装脚本）。"""
    if shutil.which("ollama"):
        return {"ok": True, "message": "ollama 已安装"}

    try:
        proc = subprocess.run(
            ["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode == 0:
            return {"ok": True, "message": "安装成功"}
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": proc.stderr[:500]},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/setup/start_ollama")
async def start_ollama():
    """启动 ollama 服务。"""
    try:
        subprocess.run(
            ["systemctl", "start", "ollama"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        # 没有 systemd 权限时尝试后台启动
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # 等待就绪
    import asyncio

    for _ in range(10):
        await asyncio.sleep(1)
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
                if resp.status_code == 200:
                    return {"ok": True}
        except Exception:
            pass

    raise HTTPException(status_code=500, detail="ollama 启动超时")


@app.post("/api/setup/set_config")
async def set_config(
    ollama_model: str = Form(None),
    whisper_model: str = Form(None),
    custom_keywords: str = Form(None),
    translate_target_lang: str = Form(None),
    baidu_appid: str = Form(None),
    baidu_appkey: str = Form(None),
    hotkey_voice: str = Form(None),
    hotkey_screenshot: str = Form(None),
    hotkey_repeat: str = Form(None),
):
    """运行时切换模型/关键词（不重启服务），同时持久化到 .config.json。"""
    global OLLAMA_MODEL, CUSTOM_KEYWORDS, WHISPER_MODEL_NAME, _whisper_model, TRANSLATE_TARGET_LANG
    global HOTKEY_VOICE, HOTKEY_SCREENSHOT, HOTKEY_REPEAT
    global BAIDU_APPID, BAIDU_APPKEY
    changed = {}
    if ollama_model and ollama_model != OLLAMA_MODEL:
        OLLAMA_MODEL = ollama_model
        changed["ollama_model"] = ollama_model
    if whisper_model and whisper_model != WHISPER_MODEL_NAME:
        WHISPER_MODEL_NAME = whisper_model
        _whisper_model = None
        changed["whisper_model"] = whisper_model
    if translate_target_lang and translate_target_lang != TRANSLATE_TARGET_LANG:
        TRANSLATE_TARGET_LANG = translate_target_lang
        changed["translate_target_lang"] = translate_target_lang
    if baidu_appid is not None and baidu_appid != BAIDU_APPID:
        BAIDU_APPID = baidu_appid
        changed["baidu_appid"] = baidu_appid
    if baidu_appkey is not None and baidu_appkey != BAIDU_APPKEY:
        BAIDU_APPKEY = baidu_appkey
        changed["baidu_appkey"] = "(已设置)"
    if hotkey_voice and hotkey_voice != HOTKEY_VOICE:
        HOTKEY_VOICE = hotkey_voice
        changed["hotkey_voice"] = hotkey_voice
    if hotkey_screenshot and hotkey_screenshot != HOTKEY_SCREENSHOT:
        HOTKEY_SCREENSHOT = hotkey_screenshot
        changed["hotkey_screenshot"] = hotkey_screenshot
    if hotkey_repeat and hotkey_repeat != HOTKEY_REPEAT:
        HOTKEY_REPEAT = hotkey_repeat
        changed["hotkey_repeat"] = hotkey_repeat
    if custom_keywords is not None:
        # 支持逗号、顿号、空格分隔
        import re

        kw_list = [
            w.strip() for w in re.split(r"[,，、\s]+", custom_keywords) if w.strip()
        ]
        CUSTOM_KEYWORDS = kw_list
        changed["custom_keywords"] = kw_list
    if changed:
        _save_config()
    return {"ok": True, "changed": changed}


@app.post("/api/setup/preload_whisper")
async def preload_whisper(model: str = Form(None)):
    """预下载并加载 Whisper 模型。模型不存在时会自动从 HuggingFace 下载。"""
    global _whisper_model, WHISPER_MODEL_NAME
    target = model or WHISPER_MODEL_NAME
    if model and model != WHISPER_MODEL_NAME:
        WHISPER_MODEL_NAME = model
        _whisper_model = None
        _save_config()

    try:
        await asyncio.to_thread(get_whisper_model)
        return {"ok": True, "model": target, "message": f"{target} 已加载就绪"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"加载模型失败: {exc}") from exc
