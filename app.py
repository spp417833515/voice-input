from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import asyncio
import tempfile
import threading
import time
import re
import soundfile as _sf
from collections import deque
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MODEL_CACHE_DIR = BASE_DIR / ".cache" / "models"
CONFIG_PATH = BASE_DIR / ".config.json"
HISTORY_PATH = BASE_DIR / ".cache" / "history.jsonl"
TOKEN_PATH = BASE_DIR / ".web_input_token"

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
        try:
            os.chmod(CONFIG_PATH, 0o600)  # 含百度 appkey，禁止其他本地用户读取
        except Exception:
            pass
    except Exception:
        pass


_load_config()


# ---------------------------------------------------------------------------
# 网页语音录入：token 鉴权 + 待注入队列（跨请求内存队列，daemon 长轮询消费）
# ---------------------------------------------------------------------------

def _get_web_token() -> str:
    """读取/生成网页录入 token，持久化到 0600 文件（供本机页面与手机页共享）。"""
    try:
        if TOKEN_PATH.exists():
            t = TOKEN_PATH.read_text().strip()
            if t:
                return t
    except Exception:
        pass
    t = secrets.token_urlsafe(18)
    try:
        TOKEN_PATH.write_text(t)
        os.chmod(TOKEN_PATH, 0o600)
    except Exception:
        pass
    return t


WEB_INPUT_TOKEN = _get_web_token()

# 待注入文本队列：submit 入队，daemon /api/web_input/poll 长轮询取走
_web_input_queue: deque[dict] = deque()
_web_input_cond = threading.Condition()

# 任务状态表：手机据此实时看到电脑端真实进度（转写→排队→投递→注入完成/失败）。
# 状态流转: queued(已转写待电脑取) → delivered(电脑已取,注入中) → done/failed
_web_jobs: dict[str, dict] = {}
_WEB_JOBS_MAX = 60


def _web_job_set(job_id: str, state: str, **kw) -> None:
    with _web_input_cond:
        job = _web_jobs.get(job_id, {})
        job.update(state=state, ts=time.time(), **kw)
        _web_jobs[job_id] = job
        if len(_web_jobs) > _WEB_JOBS_MAX:  # 超上限清理最旧
            for k in sorted(_web_jobs, key=lambda k: _web_jobs[k]["ts"])[: len(_web_jobs) - _WEB_JOBS_MAX]:
                _web_jobs.pop(k, None)


def _web_job_get(job_id: str) -> dict | None:
    with _web_input_cond:
        job = _web_jobs.get(job_id)
        return dict(job) if job else None


def _web_input_push(text: str, source: str = "web", job_id: str = "") -> None:
    with _web_input_cond:
        _web_input_queue.append(
            {"text": text, "source": source, "ts": time.time(), "job_id": job_id}
        )
        _web_input_cond.notify_all()


def _web_input_pop_wait(timeout: float) -> dict | None:
    """阻塞至多 timeout 秒取一条；无则返回 None（供长轮询）。"""
    deadline = time.time() + timeout
    with _web_input_cond:
        while not _web_input_queue:
            remain = deadline - time.time()
            if remain <= 0:
                return None
            _web_input_cond.wait(timeout=remain)
        return _web_input_queue.popleft()


app = FastAPI(title="Ollama Voice Input")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

SERVER_PORT = int(os.getenv("SERVER_PORT", "17945"))
# 本机同源白名单（CSRF Origin 校验用）
_ALLOWED_HOSTS = {
    f"127.0.0.1:{SERVER_PORT}", f"localhost:{SERVER_PORT}", "127.0.0.1", "localhost",
}
# 回环 socket 地址（判定请求是否来自本机）
_LOCALHOST_IPS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _is_local(request: Request) -> bool:
    """请求是否来自本机回环。DNS-rebinding 无法伪造 socket 对端地址，故可靠。"""
    return bool(request.client) and request.client.host in _LOCALHOST_IPS


def _lan_allowed(path: str) -> bool:
    """允许局域网访问的路径白名单：手机录音页 + 静态资源 + 带 token 的网页录入接口。

    其余一切（/api/setup/* 装软件删模型、转写、历史、poll、配置页）默认仅本机可访问。
    """
    if path == "/voice":
        return True
    if path.startswith("/static/"):
        return True
    if path in (
        "/api/web_input/submit", "/api/web_input/info", "/api/web_input/status",
        "/api/web_input/history", "/api/web_input/resend",
        "/api/v1/stt",  # 通用纯语音转文字对接端点（token 鉴权）
    ):
        return True
    return False


@app.middleware("http")
async def _access_guard(request: Request, call_next):
    """分级访问控制 + CSRF 防护。

    - 局域网白名单路径（手机录音页 / 静态资源 / 带 token 的录入接口）：放行到 LAN，
      其中 submit 由端点内 token 鉴权兜底。
    - 其余全部路径（含 /api/setup/* 危险操作、转写、历史、poll、配置页）：仅本机
      socket 可访问 —— 局域网他人即使能连上端口也拿不到这些能力。
    - 本机写操作 / setup：再叠加 Origin 校验，挡浏览器 CSRF。
    """
    path = request.url.path
    if not _lan_allowed(path) and not _is_local(request):
        return JSONResponse(status_code=403, content={"detail": "该接口仅本机可访问"})
    if _is_local(request) and (
        request.method not in ("GET", "HEAD", "OPTIONS") or path.startswith("/api/setup/")
    ):
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            if urlparse(origin).netloc not in _ALLOWED_HOSTS:
                return JSONResponse(status_code=403, content={"detail": "跨站请求被拒绝 (CSRF 防护)"})
    return await call_next(request)


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
        try:
            os.chmod(HISTORY_PATH, 0o600)  # 语音历史可能含口述隐私，禁止其他本地用户读取
        except Exception:
            pass
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
    import numpy as _np
    from faster_whisper import decode_audio

    model = get_whisper_model()
    # 将自定义关键词作为 initial_prompt 传入，提升专有名词识别率
    prompt = "，".join(CUSTOM_KEYWORDS) if CUSTOM_KEYWORDS else None

    # 用 faster-whisper 自带解码(PyAV/ffmpeg)统一把任意格式(wav/webm/opus/mp4/aac…)
    # 解码为 16kHz 单声道 float32。替代只认 wav/flac 的 soundfile —— 手机浏览器
    # MediaRecorder 产出 webm/opus，soundfile 会 "Format not recognised" 直接 500。
    try:
        audio = decode_audio(audio_path, sampling_rate=16000)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"音频解码失败（格式不支持或文件损坏）: {exc}"
        ) from exc
    audio = _np.asarray(audio, dtype=_np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio_duration = (len(audio) / 16000.0) if audio.size else 0.0

    # 静音检测：在送入 Whisper 前拦截，避免产生幻觉浪费算力
    _rms = float(_np.sqrt(_np.mean(audio ** 2))) if audio.size else 0.0
    if _rms < 0.001:
        raise HTTPException(
            status_code=400,
            detail="录音音频为静音（麦克风可能未连接或被静音）。"
            "请检查：系统设置 → 声音 → 输入，确认选择了正确的麦克风设备。",
        )

    # 短录音（<3秒）关闭 VAD 过滤，避免误判为静音丢弃
    use_vad = WHISPER_VAD_FILTER and audio_duration >= 3.0
    segments, _info = model.transcribe(
        audio,
        language=None if language == "auto" else language,
        vad_filter=use_vad,
        beam_size=5,
        initial_prompt=prompt,
    )
    # 过滤 Whisper 幻觉（短音频/静音容易产生虚假文本）
    # 策略: 段落声称的时间远超实际音频时长 → 幻觉
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


@app.get("/voice")
async def voice_page():
    """手机/其他电脑打开的录音页（局域网可访问，页内用 token 提交）。"""
    return FileResponse(STATIC_DIR / "voice.html")


@app.get("/voice_setup")
async def voice_setup_page():
    """本机专属：展示局域网录音入口 URL + token + 二维码（中间件已限本机）。"""
    return FileResponse(STATIC_DIR / "voice_setup.html")


@app.get("/api_docs")
async def api_docs_page():
    """本机专属：API 对接文档页（含 token 与示例，中间件已限本机）。"""
    return FileResponse(STATIC_DIR / "api.html")


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
    language: str = Form(WHISPER_LANGUAGE),
):
    suffix = Path(audio.filename or "recording.webm").suffix or ".webm"

    # tmp_path 在写入阶段就可能因异常残留，故用单一 try/finally 覆盖"创建+写入+转写"
    # 全过程，保证任何路径下临时文件都被清理（原实现的 unlink 在 with 之外，写入
    # 阶段抛异常会泄漏临时文件）。
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await audio.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
        raw_text = await asyncio.to_thread(transcribe_audio, tmp_path, language)
        _save_history(raw_text, raw_text, "raw", language)
        return {"raw_text": raw_text, "final_text": raw_text, "mode": "raw"}
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 网页语音录入 API：手机/其他电脑录音 → 转写 → 入队 → 本机 daemon 长轮询注入
# ---------------------------------------------------------------------------


def _local_ips() -> list[str]:
    """枚举本机局域网 IPv4（供本机 setup 页生成手机可访问的 URL）。"""
    ips: list[str] = []
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        # 回退：UDP 连一次外部地址取本机出口网卡 IP（不真正发包）
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


def _qr_data_uri(text: str) -> str | None:
    """把文本编码成二维码 PNG 的 data URI；qrcode 未安装则返回 None。"""
    try:
        import io
        import qrcode
        img = qrcode.make(text)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


@app.get("/api/web_input/config")
async def web_input_config():
    """本机专属：返回 token / 端口 / 局域网 IP + 每个 IP 的录音页 URL 与二维码。"""
    ips = _local_ips()
    entries = []
    for ip in ips:
        # https：手机浏览器只在安全上下文(https/localhost)才允许网页内录音
        url = f"https://{ip}:{SERVER_PORT}/voice?token={WEB_INPUT_TOKEN}"
        entries.append({"ip": ip, "url": url, "qr": _qr_data_uri(url)})
    return {
        "token": WEB_INPUT_TOKEN,
        "port": SERVER_PORT,
        "ips": ips,
        "entries": entries,
        "qrcode_available": bool(entries and entries[0]["qr"]),
    }


@app.get("/api/web_input/info")
async def web_input_info(token: str = ""):
    """手机录音页加载时校验 token 是否有效（局域网可访问）。"""
    if not secrets.compare_digest(token, WEB_INPUT_TOKEN):
        raise HTTPException(status_code=403, detail="token 无效")
    return {"ok": True, "language": WHISPER_LANGUAGE}


@app.post("/api/web_input/submit")
async def web_input_submit(
    audio: UploadFile = File(...),
    token: str = Form(""),
    language: str = Form(WHISPER_LANGUAGE),
):
    """手机/其他电脑提交录音：token 校验 → 转写 → 入队待本机注入。

    返回 job_id，手机据此轮询 /status 看电脑端真实注入进度。
    """
    if not secrets.compare_digest(token, WEB_INPUT_TOKEN):
        raise HTTPException(status_code=403, detail="token 无效")

    suffix = Path(audio.filename or "recording.webm").suffix or ".webm"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await audio.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
        text = await asyncio.to_thread(transcribe_audio, tmp_path, language)
        _save_history(text, text, "web", language)
        job_id = secrets.token_hex(8)
        _web_job_set(job_id, "queued", text=text)
        _web_input_push(text, source="web", job_id=job_id)
        return {"ok": True, "text": text, "job_id": job_id}
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


@app.get("/api/web_input/poll")
async def web_input_poll(wait: float = 25.0):
    """本机 daemon 长轮询：取一条待注入文本，无则等待至 wait 秒返回空。"""
    wait = max(0.0, min(wait, 55.0))
    item = await asyncio.to_thread(_web_input_pop_wait, wait)
    if item is None:
        return {"text": None}
    if item.get("job_id"):
        _web_job_set(item["job_id"], "delivered")  # 电脑已取走，进入注入中
    return {"text": item["text"], "source": item["source"], "job_id": item.get("job_id", "")}


@app.post("/api/web_input/report")
async def web_input_report(
    job_id: str = Form(...), ok: bool = Form(...), detail: str = Form(""),
):
    """本机 daemon 注入完成后回传结果（仅本机可访问，供手机 /status 读取）。"""
    _web_job_set(job_id, "done" if ok else "failed", detail=detail)
    return {"ok": True}


@app.get("/api/web_input/status")
async def web_input_status(token: str = "", job_id: str = ""):
    """手机轮询任务真实状态（局域网 + token）。"""
    if not secrets.compare_digest(token, WEB_INPUT_TOKEN):
        raise HTTPException(status_code=403, detail="token 无效")
    job = _web_job_get(job_id)
    if not job:
        return {"state": "unknown"}
    return {"state": job["state"], "detail": job.get("detail", "")}


@app.get("/api/web_input/history")
async def web_input_history(token: str = "", limit: int = 30):
    """网页端查看最近输入历史（局域网 + token）。用于"点击历史快速再次输入"。"""
    if not secrets.compare_digest(token, WEB_INPUT_TOKEN):
        raise HTTPException(status_code=403, detail="token 无效")
    limit = max(1, min(limit, 100))
    if not HISTORY_PATH.exists():
        return {"records": []}
    try:
        lines = [l for l in HISTORY_PATH.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
    except Exception:
        return {"records": []}
    out = []
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except Exception:
            continue
        text = (r.get("final_text") or r.get("raw_text") or "").strip()
        if not text:
            continue
        out.append({"text": text, "timestamp": r.get("timestamp", ""), "mode": r.get("mode", "")})
        if len(out) >= limit:
            break
    return {"records": out}


@app.post("/api/web_input/resend")
async def web_input_resend(token: str = Form(""), text: str = Form("")):
    """把选中的历史文本再次注入本机焦点窗口（局域网 + token），复用注入队列。"""
    if not secrets.compare_digest(token, WEB_INPUT_TOKEN):
        raise HTTPException(status_code=403, detail="token 无效")
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="文本为空")
    job_id = secrets.token_hex(8)
    _web_job_set(job_id, "queued", text=text)
    _web_input_push(text, source="resend", job_id=job_id)
    return {"ok": True, "job_id": job_id}


# ---------------------------------------------------------------------------
# 通用对接 API（供其他项目/设备把本服务当作纯语音转文字服务调用）
# ---------------------------------------------------------------------------


@app.post("/api/v1/stt")
async def v1_stt(
    audio: UploadFile = File(...),
    token: str = Form(""),
    language: str = Form(WHISPER_LANGUAGE),
):
    """通用语音转文字端点（token 鉴权，局域网可用）。

    传音频（wav/webm/opus/mp4/… 任意格式）→ 返回文本。
    纯转写：不注入本机、不入队、不写历史，与网页录入互不影响，供外部项目对接。
    language 可传具体语言(zh/en/…)或 "auto"。
    """
    if not secrets.compare_digest(token, WEB_INPUT_TOKEN):
        raise HTTPException(status_code=403, detail="token 无效")
    suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await audio.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
        text = await asyncio.to_thread(transcribe_audio, tmp_path, language)
        return {"ok": True, "text": text, "language": language}
    finally:
        if tmp_path:
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
        # 自动检测多语言（英+中+日+韩）。OCR 是同步阻塞调用，必须丢线程池，
        # 否则冻结整个事件循环 → daemon 的转写等所有请求连锁卡死。
        raw_text = (await asyncio.to_thread(
            pytesseract.image_to_string, img, lang="eng+chi_sim+jpn"
        )).strip()
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
        # 翻译走第三方网络库(同步阻塞)，同样丢线程池，避免冻结事件循环。
        translated, engine = await asyncio.to_thread(_translate_text, raw_text, target)
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
            ver = await asyncio.to_thread(
                subprocess.run,
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
    """尝试安装 ollama（调用官方安装脚本）。

    安全说明：服务器绑定 127.0.0.1:17945，仅本地可访问，无远程执行风险。
    """
    if shutil.which("ollama"):
        return {"ok": True, "message": "ollama 已安装"}

    try:
        # 300s 的同步安装脚本必须丢线程池，否则冻结事件循环整整 5 分钟。
        proc = await asyncio.to_thread(
            subprocess.run,
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
        await asyncio.to_thread(
            subprocess.run,
            ["systemctl", "start", "ollama"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        # 没有 systemd 权限时尝试后台启动（Popen 只 spawn 不阻塞）
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


_WHISPER_TAGS = ("large-v3-turbo", "large-v3", "large-v2", "large",
                 "medium", "small", "base", "tiny")


def _whisper_dir_tag(name: str) -> str | None:
    """把模型目录名归一到最具体的已知标签（与 setup_check 同口径）。"""
    n = name.lower()
    for t in _WHISPER_TAGS:
        if t in n:
            return t
    return None


@app.post("/api/setup/delete_whisper")
async def delete_whisper(model: str = Form(...)):
    """删除已下载的 Whisper 模型目录（精确标签匹配，杜绝子串误删多个模型）。"""
    global _whisper_model
    import shutil

    model = (model or "").strip().lower()
    if model not in _WHISPER_TAGS:
        # 原实现用 `model in p.name` 子串匹配，传 "v"/"e" 会一次删掉多个模型目录。
        raise HTTPException(status_code=400, detail=f"非法或未知的模型标签: {model!r}")

    if not MODEL_CACHE_DIR.exists():
        raise HTTPException(status_code=404, detail="模型缓存目录不存在")

    # 精确匹配：目录归一化后的标签必须等于请求标签（"large-v3" 不会误删 "large-v3-turbo"）
    deleted = False
    for p in MODEL_CACHE_DIR.iterdir():
        if p.is_dir() and _whisper_dir_tag(p.name) == model:
            shutil.rmtree(p)
            deleted = True

    if not deleted:
        raise HTTPException(status_code=404, detail=f"未找到模型 {model}")

    # 如果删除的是当前使用的模型，清除缓存
    if model == WHISPER_MODEL_NAME:
        _whisper_model = None

    return {"ok": True, "model": model}
