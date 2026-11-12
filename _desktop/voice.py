"""按住说话：绑定本句 job_id 与按下时焦点 → 开麦推流识别会话 → 松开 finish → 定稿入业务。

- 过短（<0.15s）：取消会话，不调业务。
- 看门狗 120s：由平台强制调用 stop()（= finish，不是 cancel）。
- 每句一个上传线程，按 chunk 顺序发送；第二句不会覆盖第一句的焦点或会话。
不补句号、不润色、不注入：定稿后的处理全在服务端业务与 jobs 投递。
"""
from __future__ import annotations

import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from _capture import AudioSource, SessionCapture
from _contracts import ErrorCode
from _desktop.api import ApiError
from _desktop.status import PlatformStatus
from _focus import FocusProbe


def new_job_id() -> str:
    return f"desk-{uuid.uuid4().hex}"


class SessionUpload:
    """一句话的会话上传：create → chunks → finish → utterances（或 cancel）。单线程保证顺序。"""

    def __init__(self, api, status: PlatformStatus, *, job_id: str, language: str | None) -> None:
        self.api = api
        self.status = status
        self.job_id = job_id
        self.language = language
        self.session_id: str | None = None
        self.outcome: str | None = None
        self._items: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"session-{job_id}", daemon=True)

    def begin(self) -> None:
        self._thread.start()

    def chunk(self, index: int, pcm: bytes) -> None:
        self._items.put(("chunk", index, pcm))

    def complete(self, focus_id: str) -> None:
        self._items.put(("finish", focus_id))

    def cancel(self) -> None:
        self._items.put(("cancel",))

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            self.session_id = self.api.create_session(language=self.language)
        except Exception as exc:
            self._fail(f"识别会话无法开始：{_reason(exc)}")
            self._drain_until_end()
            return
        while True:
            item = self._items.get()
            kind = item[0]
            try:
                if kind == "chunk":
                    self.api.upload_chunk(self.session_id, item[1], item[2])
                    continue
                if kind == "cancel":
                    self.api.cancel_session(self.session_id)
                    self.outcome = "cancelled"
                    return
                self.api.finish_session(self.session_id)
                self.api.submit_utterance(job_id=self.job_id, session_id=self.session_id, focus_id=item[1])
                self.outcome = "submitted"
                return
            except Exception as exc:
                self._fail(f"识别失败：{_reason(exc)}")
                self._abandon()
                self._drain_until_end()
                return

    def _abandon(self) -> None:
        try:
            self.api.cancel_session(self.session_id)
        except Exception:
            pass  # 会话可能已因错误关闭；结果已经以错误可见

    def _drain_until_end(self) -> None:
        while True:
            item = self._items.get()
            if item[0] in ("finish", "cancel"):
                return

    def _fail(self, message: str) -> None:
        self.outcome = "failed"
        self.status.error(ErrorCode.asr_unavailable, message)


def _reason(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return exc.message
    return str(exc)[:120] or type(exc).__name__


@dataclass
class _Active:
    capture: SessionCapture
    upload: SessionUpload


class VoiceFlow:
    def __init__(
        self,
        *,
        api,
        status: PlatformStatus,
        probe: FocusProbe,
        source_factory: Callable[[], AudioSource],
        beep: Callable[[float], None] = lambda freq: None,
        language: Callable[[], str | None] = lambda: None,
        job_ids: Callable[[], str] = new_job_id,
    ) -> None:
        self.api = api
        self.status = status
        self.probe = probe
        self.source_factory = source_factory
        self.beep = beep
        self.language = language
        self.job_ids = job_ids
        self._active: _Active | None = None
        self.last_upload: SessionUpload | None = None

    def start(self) -> bool:
        """GTK 主线程调用；只开麦与起线程，不做阻塞网络调用。开麦失败返回 False 并可见。"""
        if self._active is not None:
            return True
        upload = SessionUpload(self.api, self.status, job_id=self.job_ids(), language=self.language())
        capture = SessionCapture(self.source_factory(), self.probe, on_chunk=upload.chunk)
        try:
            capture.start()
        except Exception as exc:
            print(f"[voice] 音频流启动失败: {exc}", flush=True)
            self.status.error(ErrorCode.dependency_failed, f"麦克风打开失败：{str(exc)[:80]}")
            return False
        self.beep(400)  # 低音提示: 开始录音（流启动后再播，避免设备冲突）
        upload.begin()
        self._active = _Active(capture, upload)
        self.last_upload = upload
        return True

    def stop(self) -> None:
        """松开或看门狗 finish。"""
        active = self._active
        self._active = None
        if active is None:
            return
        result = active.capture.finish()
        self.beep(700)  # 高音提示: 结束录音
        print(f"  录音: 时长 {result.duration_s:.2f}s, {result.chunks} 块", flush=True)
        if not result.accepted:
            active.upload.cancel()
            return
        focus = result.focus.focus_id if result.focus is not None else 0
        active.upload.complete(str(focus) if focus else "")
