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


def _load_config() -> None:
    """从 .config.json 加载持久化配置。"""
    global OLLAMA_MODEL, CUSTOM_KEYWORDS
    if not CONFIG_PATH.exists():
        return
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        OLLAMA_MODEL = cfg.get("ollama_model", OLLAMA_MODEL)
        CUSTOM_KEYWORDS = cfg.get("custom_keywords", CUSTOM_KEYWORDS)
    except Exception:
        pass


def _save_config() -> None:
    """将当前模型配置写入 .config.json（合并已有字段）。"""
    cfg = {
        "ollama_model": OLLAMA_MODEL,
        "custom_keywords": CUSTOM_KEYWORDS,
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
    segments, _info = model.transcribe(
        audio_path,
        language=None if language == "auto" else language,
        vad_filter=WHISPER_VAD_FILTER,
        beam_size=5,
    )
    text = " ".join(segment.text.strip() for segment in segments).strip()
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

    if mode == "polish":
        system = (
            "你是语音输入纠错工具。"
            "规则：1.补全标点 2.修正错别字 3.清理口语噪声词（嗯、啊、那个）"
        )
        if CUSTOM_KEYWORDS:
            kw_str = "、".join(CUSTOM_KEYWORDS)
            system += f"4.以下为常用专有名词，遇到发音相近的错误请优先纠正为这些词：{kw_str}。"
        system += (
            "禁止：解释、补充、改变原意、添加任何说明文字。"
            "直接输出修正后的文本，不要输出其他任何内容。"
        )
    elif mode == "translate_en":
        system = (
            "你是一个翻译助手。"
            "请把用户给出的中文语音转写结果翻译成自然、简洁的英文。"
            "只输出最终英文，不要解释。"
        )
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
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/setup")
async def setup_page():
    return FileResponse(STATIC_DIR / "setup.html")


@app.get("/history")
async def history_page():
    return FileResponse(STATIC_DIR / "history.html")


# ---------------------------------------------------------------------------
# 业务 API
# ---------------------------------------------------------------------------


@app.get("/api/status")
async def status():
    ollama_ok = False
    installed_models = []
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
            ollama_ok = True
            installed_models = [m["name"] for m in response.json().get("models", [])]
    except Exception:
        ollama_ok = False

    return {
        "whisper_model": WHISPER_MODEL_NAME,
        "whisper_device": WHISPER_DEVICE,
        "whisper_language": WHISPER_LANGUAGE,
        "ollama_model": OLLAMA_MODEL,
        "ollama_ok": ollama_ok,
        "custom_keywords": CUSTOM_KEYWORDS,
    }


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
        file_size = Path(tmp_path).stat().st_size
        if file_size < 1024:
            raise HTTPException(
                status_code=400,
                detail="音频文件太小，录音时间可能不足。请按住按钮至少 1 秒再松开。",
            )
        raw_text = await asyncio.to_thread(transcribe_audio, tmp_path, language)
        final_text = await call_ollama(mode, raw_text)
        _save_history(raw_text, final_text, mode, language)
        return {"raw_text": raw_text, "final_text": final_text, "mode": mode}
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/ocr_translate")
async def ocr_translate(
    image: UploadFile = File(...),
    target_lang: str = Form("zh"),
):
    """tesseract OCR 提取文字 + Google 翻译。完全不用大模型。"""
    import pytesseract
    from deep_translator import GoogleTranslator
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

    # ---- 第 2 步：Google 翻译 ----
    lang_map = {"zh": "zh-CN", "en": "en", "ja": "ja", "ko": "ko"}
    target = lang_map.get(target_lang, "zh-CN")

    try:
        translated = GoogleTranslator(source="auto", target=target).translate(raw_text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Google 翻译失败: {exc}") from exc

    if not translated:
        raise HTTPException(status_code=502, detail="翻译没有返回结果")

    return {"text": translated, "model": "tesseract + GoogleTranslate"}


# ---------------------------------------------------------------------------
# Setup API —— 环境自检 / 模型管理
# ---------------------------------------------------------------------------


@app.get("/api/setup/check")
async def setup_check():
    """一次性检查所有组件状态。"""
    result = {
        "ollama": {"installed": False, "running": False, "version": None},
        "ollama_models": [],
        "whisper": {
            "model": WHISPER_MODEL_NAME,
            "downloaded": False,
        },
        "system_tools": {},
        "config": {
            "ollama_model": OLLAMA_MODEL,
            "whisper_model": WHISPER_MODEL_NAME,
            "whisper_device": WHISPER_DEVICE,
            "whisper_language": WHISPER_LANGUAGE,
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

    # 3. whisper 模型是否已缓存
    if MODEL_CACHE_DIR.exists():
        # faster-whisper 下载的模型目录名包含模型名
        for p in MODEL_CACHE_DIR.iterdir():
            if WHISPER_MODEL_NAME in p.name:
                result["whisper"]["downloaded"] = True
                break

    # 4. 系统工具检查
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
    custom_keywords: str = Form(None),
):
    """运行时切换模型/关键词（不重启服务），同时持久化到 .config.json。"""
    global OLLAMA_MODEL, CUSTOM_KEYWORDS
    changed = {}
    if ollama_model and ollama_model != OLLAMA_MODEL:
        OLLAMA_MODEL = ollama_model
        changed["ollama_model"] = ollama_model
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
