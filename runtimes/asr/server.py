"""Sidecar HTTP：路径与 `_asr.AsrSidecarClient` 一致。不在 import 时加载官方引擎。"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from collections.abc import Callable, Sequence

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from _contracts import (
    ASR_STREAMING_BLOCKERS,
    ErrorCode,
    LogicError,
    RecognizeSessionCreate,
    RecognizeSessionCreated,
    SessionClosedEvent,
    SessionClosedReason,
    SessionErrorEvent,
    SessionFinalEvent,
    SessionPartialEvent,
    SttResponse,
)
from runtimes.asr.engine import AsrEngine
from runtimes.asr.protocol import (
    OFFICIAL_PARTIAL_POLICY,
    SIDECAR_CHUNK_HEADER,
    SIDECAR_SESSIONS_PATH,
    SIDECAR_TRANSCRIBE_PATH,
)
from runtimes.asr.scheduler import EngineScheduler

Clock = Callable[[], float]


class _HubSession:
    def __init__(self, session_id: str, now: float) -> None:
        self.session_id = session_id
        self.created_at = now
        self.last_activity = now
        self.state = "open"
        self.subscribers: list[queue.Queue] = []
        self.engine_busy = False


def _error(status: int, code: ErrorCode, message: str, details=None) -> JSONResponse:
    body = {"ok": False, "error": code.value if hasattr(code, "value") else str(code), "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status, content=body)


def _logic_status(exc: LogicError) -> int:
    mapping = {
        ErrorCode.invalid_request: 400,
        ErrorCode.not_found: 404,
        ErrorCode.conflict: 409,
        ErrorCode.timeout: 504,
        ErrorCode.asr_unavailable: 503,
    }
    return mapping.get(exc.code, 500)


def wav_to_pcm_s16le(data: bytes, filename: str | None = None) -> bytes:
    """将 sidecar 收到的整段音频转为 s16le。支持 raw PCM 或 WAV。不写磁盘。"""
    if data.startswith(b"RIFF") and b"WAVE" in data[:16]:
        import io
        import wave

        with wave.open(io.BytesIO(data), "rb") as handle:
            frames = handle.readframes(handle.getnframes())
            width = handle.getsampwidth()
            rate = handle.getframerate()
            channels = handle.getnchannels()
            if width == 2 and channels == 1 and rate == 16000:
                return frames
            try:
                import numpy as np
            except ImportError as exc:
                raise LogicError(ErrorCode.invalid_request, "整段转写需要 16kHz mono s16le WAV") from exc
            raw = np.frombuffer(frames, dtype="<i2")
            if channels > 1:
                raw = raw.reshape(-1, channels).mean(axis=1).astype(np.int16)
            if rate != 16000:
                # 线性重采样仅用于把整段 WAV 收成 16kHz，不是第二套识别引擎。
                x = raw.astype(np.float32)
                n = int(round(len(x) * 16000 / rate))
                if n <= 0:
                    return b""
                t = np.linspace(0, len(x) - 1, n)
                x = np.interp(t, np.arange(len(x)), x)
                raw = np.clip(x, -32768, 32767).astype(np.int16)
            return raw.tobytes()
    if len(data) % 2 != 0:
        raise LogicError(ErrorCode.invalid_request, "音频编码必须是 s16le PCM 或 16kHz WAV")
    return data



def _iter_engine_partials(event: object) -> list[SessionPartialEvent]:
    """引擎对一个 chunk 可返回 0、1 或多个 partial；官方实现仍是单个或 None。"""
    if event is None:
        return []
    if isinstance(event, SessionPartialEvent):
        return [event]
    if isinstance(event, Sequence) and not isinstance(event, (str, bytes, bytearray)):
        items: list[SessionPartialEvent] = []
        for item in event:
            if not isinstance(item, SessionPartialEvent):
                raise LogicError(ErrorCode.internal, "引擎 push_pcm 序列中含有非 SessionPartialEvent")
            items.append(item)
        return items
    raise LogicError(ErrorCode.internal, "引擎 push_pcm 必须返回 SessionPartialEvent、序列或 None")


def create_sidecar_app(
    engine: AsrEngine,
    *,
    scheduler: EngineScheduler | None = None,
    clock: Clock = time.monotonic,
    idle_s: float = 180.0,
    sse_queue_max: int = 32,
    blockers: tuple[str, ...] = ASR_STREAMING_BLOCKERS,
) -> FastAPI:
    sched = scheduler or EngineScheduler()
    app = FastAPI(title="ollama-voice-input ASR sidecar", docs_url=None, redoc_url=None)
    sessions: dict[str, _HubSession] = {}
    guard = threading.RLock()

    def publish(session: _HubSession, event) -> None:
        for item in list(session.subscribers):
            try:
                item.put_nowait(event)
            except queue.Full:
                # 满则可见终止：放入不了就标记由生成器结束；不静默丢。
                try:
                    item.put_nowait(
                        SessionErrorEvent(
                            session_id=session.session_id,
                            error=ErrorCode.internal,
                            message="SSE 队列已满，连接已断开",
                        )
                    )
                except queue.Full:
                    pass

    def get_session(session_id: str) -> _HubSession:
        with guard:
            session = sessions.get(session_id)
            if session is None or session.state != "open":
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            return session

    def close_session(session: _HubSession, reason: SessionClosedReason, error: SessionErrorEvent | None = None) -> None:
        with guard:
            session.state = reason.value
        if error is not None:
            publish(session, error)
        publish(session, SessionClosedEvent(session_id=session.session_id, reason=reason))
        if engine.has_stream(session.session_id):
            try:
                if reason == SessionClosedReason.cancelled or reason == SessionClosedReason.disconnected:
                    engine.cancel_stream(session.session_id)
            except LogicError:
                pass
        if sched.active_stream_id == session.session_id:
            sched.release()
        with guard:
            sessions.pop(session.session_id, None)

    @app.exception_handler(LogicError)
    async def _on_logic(_request: Request, exc: LogicError):
        return _error(_logic_status(exc), exc.code, exc.message, exc.details)

    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "loaded": bool(getattr(engine, "loaded", False)),
            "bind": "127.0.0.1",
            "unmet_blockers": list(blockers),
            "partial_policy": OFFICIAL_PARTIAL_POLICY,
        }

    @app.post(SIDECAR_TRANSCRIBE_PATH, response_model=SttResponse)
    async def transcribe(
        audio: UploadFile | None = File(default=None),
        language: str | None = Form(default=None),
        context: str | None = Form(default=None),
    ) -> SttResponse:
        if audio is None:
            raise LogicError(ErrorCode.invalid_request, "必须提供音频文件")
        data = await audio.read()
        pcm = wav_to_pcm_s16le(data, audio.filename)
        if not getattr(engine, "loaded", True):
            raise LogicError(ErrorCode.asr_unavailable, "官方引擎尚未 load")
        sched.acquire_batch()
        try:
            return engine.transcribe(pcm, language=language, context=context)
        finally:
            sched.release()

    @app.post(SIDECAR_SESSIONS_PATH, response_model=RecognizeSessionCreated)
    def create_session(body: RecognizeSessionCreate | None = None) -> RecognizeSessionCreated:
        if not getattr(engine, "loaded", True):
            raise LogicError(ErrorCode.asr_unavailable, "官方引擎尚未 load")
        payload = body or RecognizeSessionCreate()
        session_id = str(uuid.uuid4())
        sched.acquire_stream(session_id)
        try:
            engine.start_stream(session_id, language=payload.language, context=payload.context)
        except Exception:
            sched.release()
            raise
        with guard:
            sessions[session_id] = _HubSession(session_id, clock())
        return RecognizeSessionCreated(session_id=session_id)

    @app.post(f"{SIDECAR_SESSIONS_PATH}/{{session_id}}/chunks")
    async def upload_chunk(
        session_id: str,
        request: Request,
        x_chunk_index: str | None = Header(default=None, alias=SIDECAR_CHUNK_HEADER),
    ):
        if x_chunk_index is None or not str(x_chunk_index).strip():
            raise LogicError(ErrorCode.invalid_request, f"缺少 {SIDECAR_CHUNK_HEADER}")
        try:
            index = int(x_chunk_index)
        except ValueError as exc:
            raise LogicError(ErrorCode.invalid_request, "chunk index 必须是整数") from exc
        if index < 0:
            raise LogicError(ErrorCode.invalid_request, "chunk index 不能为负")
        pcm = await request.body()
        session = get_session(session_id)
        session.last_activity = clock()
        event = engine.push_pcm(session_id, pcm)
        for item in _iter_engine_partials(event):
            publish(session, item)
        return {"accepted": True, "index": index}

    @app.get(f"{SIDECAR_SESSIONS_PATH}/{{session_id}}/events")
    async def events(session_id: str, request: Request) -> StreamingResponse:
        session = get_session(session_id)
        q: queue.Queue = queue.Queue(maxsize=sse_queue_max)
        session.subscribers.append(q)
        hook = getattr(engine, "on_events_subscribed", None)
        if callable(hook):
            for item in hook(session_id) or []:
                if item is not None:
                    publish(session, item)

        async def generate():
            try:
                while True:
                    if await request.is_disconnected():
                        if session.state == "open":
                            close_session(
                                session,
                                SessionClosedReason.disconnected,
                                SessionErrorEvent(
                                    session_id=session_id,
                                    error=ErrorCode.invalid_request,
                                    message="SSE 在 finish 前断开，不调用 finish、不定稿",
                                ),
                            )
                        break
                    try:
                        event = await asyncio.to_thread(q.get, True, 0.2)
                    except queue.Empty:
                        continue
                    payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
                    name = payload.get("event") if isinstance(payload, dict) else None
                    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    yield (f"event: {name}\ndata: {data}\n\n" if name else f"data: {data}\n\n")
                    if getattr(event, "event", None) == "closed":
                        return
            finally:
                session.subscribers = [item for item in session.subscribers if item is not q]

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post(f"{SIDECAR_SESSIONS_PATH}/{{session_id}}/finish")
    def finish(session_id: str):
        session = get_session(session_id)
        try:
            final = engine.finish_stream(session_id)
        except LogicError as exc:
            # 引擎已丢弃该流；关闭会话并释放调度槽，让订阅者看到 error+closed，而不是挂到空闲超时。
            close_session(
                session,
                SessionClosedReason.error,
                SessionErrorEvent(session_id=session_id, error=exc.code, message=exc.message),
            )
            raise
        if not isinstance(final, SessionFinalEvent):
            raise LogicError(ErrorCode.asr_unavailable, "finish 必须返回 final 事件")
        publish(session, final)
        close_session(session, SessionClosedReason.finished)
        # close_session 会尝试 cancel 已弹出的会话，has_stream 应为 False
        return final.model_dump(mode="json")

    @app.post(f"{SIDECAR_SESSIONS_PATH}/{{session_id}}/cancel", response_model=SessionClosedEvent)
    def cancel(session_id: str) -> SessionClosedEvent:
        session = get_session(session_id)
        if engine.has_stream(session_id):
            engine.cancel_stream(session_id)
        closed = SessionClosedEvent(session_id=session_id, reason=SessionClosedReason.cancelled)
        close_session(session, SessionClosedReason.cancelled)
        return closed

    def sweep() -> None:
        now = clock()
        with guard:
            stale = [
                session
                for session in sessions.values()
                if session.state == "open" and now - session.last_activity >= idle_s
            ]
        for session in stale:
            close_session(
                session,
                SessionClosedReason.disconnected,
                SessionErrorEvent(
                    session_id=session.session_id,
                    error=ErrorCode.timeout,
                    message="会话空闲超时，已释放引擎与音频缓存",
                ),
            )

    app.state.engine = engine
    app.state.scheduler = sched
    app.state.sessions = sessions
    app.state.sweep = sweep
    app.state.blockers = blockers
    return app
