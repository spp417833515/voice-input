"""把 `_asr.AsrSidecarClient` 接到 RecognizeSessionHub 的 StreamingAsrProvider。

应用会话 id 与 sidecar 会话 id 做映射，不改公开契约字段。
通过单一 iter_events 消费者把 sidecar SSE 桥到会话 sink；push_pcm 只发送音频，不阻塞等事件。
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import httpx

from _asr import AsrSidecarClient
from _contracts import (
    ErrorCode,
    LogicError,
    RecognizeSessionCreate,
    SessionClosedEvent,
    SessionClosedReason,
    SessionErrorEvent,
    SessionFinalEvent,
    SessionPartialEvent,
    SessionSSEEvent,
)

if TYPE_CHECKING:
    from _v3.sessions import SessionCaptionSink

_READY_TIMEOUT_S = 8.0
_SETTLE_TIMEOUT_S = 15.0
_JOIN_TIMEOUT_S = 5.0


class _SidecarEventBridge:
    """一个 sidecar 会话对应一条事件流、一个读线程。按到达顺序分发，不重排。"""

    def __init__(
        self,
        *,
        client: AsrSidecarClient,
        remote_id: str,
        app_session_id: str,
        sink: SessionCaptionSink | None,
    ) -> None:
        self.client = client
        self.remote_id = remote_id
        self.app_session_id = app_session_id
        self.sink = sink
        self.final: SessionFinalEvent | None = None
        self.closed: SessionClosedEvent | None = None
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._stop = threading.Event()
        self._stream: httpx.Response | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        name = f"sidecar-sse-{self.app_session_id[:8]}"
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=_READY_TIMEOUT_S):
            self.stop()
            raise LogicError(ErrorCode.timeout, "sidecar 事件流未能在时限内就绪")
        if self.error is not None:
            err = self.error
            self.stop()
            if isinstance(err, LogicError):
                raise err
            raise LogicError(ErrorCode.asr_unavailable, f"sidecar 事件流启动失败: {err}") from err

    def wait_settled(self, *, timeout: float = _SETTLE_TIMEOUT_S) -> None:
        if not self._done.wait(timeout=timeout):
            raise LogicError(ErrorCode.timeout, "sidecar 事件流未能在 finish/cancel 后收束")
        self._join()
        if self.error is not None:
            err = self.error
            if isinstance(err, LogicError):
                raise err
            raise LogicError(ErrorCode.asr_unavailable, f"sidecar 事件流失败: {err}") from err

    def stop(self) -> None:
        self._stop.set()
        self._close_stream()
        self._join()

    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _attach(self, response: httpx.Response) -> None:
        self._stream = response
        self._ready.set()

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.close()
        except Exception:
            pass

    def _join(self) -> None:
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=_JOIN_TIMEOUT_S)
        if thread.is_alive():
            if self.error is None:
                self.error = LogicError(ErrorCode.internal, "sidecar 事件读线程未能退出，存在残留")

    def _run(self) -> None:
        try:
            for event in self.client.iter_events(self.remote_id, attach_response=self._attach):
                if self._stop.is_set():
                    break
                terminal = self._dispatch(event)
                if terminal:
                    break
            else:
                if not self._stop.is_set() and self.closed is None and self.error is None:
                    self._record_error(
                        LogicError(ErrorCode.asr_unavailable, "sidecar 事件流在 closed 前结束，读线程已退出")
                    )
        except LogicError as exc:
            self._record_error(exc)
        except httpx.HTTPError as exc:
            if self._stop.is_set():
                pass
            else:
                self._record_error(LogicError(ErrorCode.asr_unavailable, f"ASR 事件流中断: {exc}"))
        except Exception as exc:
            if self._stop.is_set():
                pass
            else:
                self._record_error(LogicError(ErrorCode.internal, f"sidecar 事件消费者异常: {exc}"))
        finally:
            self._stream = None
            self._ready.set()
            self._done.set()

    def _record_error(self, exc: BaseException) -> None:
        with self._lock:
            if self.error is None:
                self.error = exc
        if isinstance(exc, LogicError) and self.sink is not None:
            try:
                self.sink.publish_provider_error(
                    self.app_session_id,
                    SessionErrorEvent(
                        session_id=self.app_session_id,
                        error=exc.code,
                        message=exc.message,
                        details=exc.details,
                    ),
                )
            except Exception:
                pass

    def _dispatch(self, event: SessionSSEEvent) -> bool:
        """按 sidecar 到达顺序处理。partial 立即交给 sink；final 只保留给 finish()。返回是否结束。"""
        publish: SessionPartialEvent | None = None
        error: SessionErrorEvent | None = None
        with self._lock:
            kind = getattr(event, "event", None)
            if kind == "partial":
                if not isinstance(event, SessionPartialEvent):
                    self.error = LogicError(ErrorCode.internal, "收到标记为 partial 的非法事件")
                    return True
                if self.final is not None or self.closed is not None:
                    self.error = LogicError(ErrorCode.internal, "final/closed 之后又收到 partial，拒绝乱序")
                    return True
                publish = event.model_copy(update={"session_id": self.app_session_id})
            elif kind == "final":
                if not isinstance(event, SessionFinalEvent):
                    self.error = LogicError(ErrorCode.internal, "收到标记为 final 的非法事件")
                    return True
                if self.final is not None:
                    self.error = LogicError(ErrorCode.internal, "sidecar 对同一会话发出了重复 final")
                    return True
                if self.closed is not None:
                    self.error = LogicError(ErrorCode.internal, "closed 之后又收到 final，拒绝乱序")
                    return True
                self.final = event.model_copy(update={"session_id": self.app_session_id})
                return False
            elif kind == "error":
                if isinstance(event, SessionErrorEvent):
                    error = event.model_copy(update={"session_id": self.app_session_id})
                else:
                    self.error = LogicError(ErrorCode.internal, "收到标记为 error 的非法事件")
                    return True
            elif kind == "closed":
                if not isinstance(event, SessionClosedEvent):
                    self.error = LogicError(ErrorCode.internal, "收到标记为 closed 的非法事件")
                    return True
                self.closed = event
                return True
            else:
                self.error = LogicError(ErrorCode.asr_unavailable, f"未知的 sidecar SSE event: {kind!r}")
                return True
        if publish is not None and self.sink is not None:
            self.sink.publish_partial(self.app_session_id, publish)
        if error is not None:
            with self._lock:
                if self.error is None:
                    self.error = LogicError(error.error, error.message, details=error.details)
            if self.sink is not None:
                self.sink.publish_provider_error(self.app_session_id, error)
            return True
        return False


class SidecarStreamProvider:
    official_engine = False

    def __init__(self, client: AsrSidecarClient) -> None:
        self.client = client
        self._map: dict[str, str] = {}
        self._bridges: dict[str, _SidecarEventBridge] = {}
        self._guard = threading.Lock()

    def start(self, session_id: str, body: RecognizeSessionCreate, *, sink: SessionCaptionSink | None = None) -> None:
        created = self.client.create_session(body)
        remote = created.session_id
        bridge = _SidecarEventBridge(client=self.client, remote_id=remote, app_session_id=session_id, sink=sink)
        with self._guard:
            self._map[session_id] = remote
            self._bridges[session_id] = bridge
        try:
            bridge.start()
        except Exception:
            with self._guard:
                self._bridges.pop(session_id, None)
                self._map.pop(session_id, None)
            try:
                self.client.cancel(remote)
            except Exception:
                pass
            bridge.stop()
            raise

    def _sid(self, session_id: str) -> str:
        try:
            return self._map[session_id]
        except KeyError as exc:
            raise KeyError(f"没有对应的 sidecar 会话: {session_id}") from exc

    def push_pcm(self, session_id: str, index: int, pcm: bytes) -> SessionPartialEvent | None:
        # 只发送音频。字幕由唯一的 iter_events 消费者按到达顺序交给 sink，不在此处阻塞等待。
        self.client.send_chunk(self._sid(session_id), index, pcm)
        return None

    def finish(self, session_id: str) -> SessionFinalEvent:
        """一次定稿。成功或失败都在 finally 中幂等收束读线程、HTTP 事件流和本地映射。

        失败（HTTP 503、事件流提前断开、SSE error、终稿不一致等）原样抛出，不返回 final、不入队；
        若 SSE 尚未把错误投到应用会话，这里补投一次 error（会话随之 closed(error)，重复投递无副作用）。
        """
        remote = self._sid(session_id)
        with self._guard:
            bridge = self._bridges.get(session_id)
        try:
            result = self.client.finish(remote)
            if bridge is not None:
                bridge.wait_settled()
            text, language = self._resolve_final(session_id, result, bridge)
        except BaseException as exc:
            self._fail_finish(session_id, remote, bridge, exc)
            raise
        finally:
            self._release(session_id, bridge)
        return SessionFinalEvent(session_id=session_id, text=text, language=language)

    def _fail_finish(
        self,
        session_id: str,
        remote: str,
        bridge: _SidecarEventBridge | None,
        exc: BaseException,
    ) -> None:
        """finish 失败：让应用会话可见地以 error 关闭，并请求 sidecar 丢弃会话。"""
        error = exc if isinstance(exc, LogicError) else LogicError(ErrorCode.asr_unavailable, f"sidecar finish 失败: {exc}")
        if bridge is not None:
            bridge._record_error(error)
        # sidecar 在定稿失败时自己已关闭会话，这时 cancel 多半得到 not_found。任何 cancel 结果都不替换
        # 原失败原因，但也不静默丢弃：作为注记附在原异常上，供日志区分「已关闭」与其他清理失败。
        try:
            self.client.cancel(remote)
        except LogicError as cancel_exc:
            exc.add_note(f"sidecar cancel 清理结果: {cancel_exc.code}: {cancel_exc.message}")
        except Exception as cancel_exc:
            exc.add_note(f"sidecar cancel 清理失败: {cancel_exc!r}")

    def _release(self, session_id: str, bridge: _SidecarEventBridge | None) -> None:
        """幂等：停读线程（关闭 HTTP 事件流）并移除本地映射。"""
        if bridge is not None and (bridge.alive() or bridge._stream is not None):
            bridge.stop()
        self._drop(session_id)

    def cancel(self, session_id: str) -> None:
        with self._guard:
            remote = self._map.get(session_id)
            bridge = self._bridges.get(session_id)
        if remote is None and bridge is None:
            return
        cancel_error: BaseException | None = None
        if remote is not None:
            try:
                self.client.cancel(remote)
            except LogicError as exc:
                if exc.code != ErrorCode.not_found:
                    cancel_error = exc
            except Exception as exc:
                cancel_error = exc
        if bridge is not None:
            try:
                bridge.wait_settled(timeout=_JOIN_TIMEOUT_S)
            except LogicError:
                bridge.stop()
            if bridge.alive():
                bridge.stop()
        self._drop(session_id)
        if cancel_error is not None and not isinstance(cancel_error, LogicError):
            raise LogicError(ErrorCode.asr_unavailable, f"sidecar cancel 失败: {cancel_error}") from cancel_error
        if isinstance(cancel_error, LogicError):
            raise cancel_error

    def close(self) -> None:
        with self._guard:
            ids = list(self._map.keys())
        for session_id in ids:
            try:
                self.cancel(session_id)
            except Exception:
                with self._guard:
                    bridge = self._bridges.get(session_id)
                if bridge is not None:
                    bridge.stop()
                self._drop(session_id)

    def leftover_threads(self) -> list[str]:
        with self._guard:
            bridges = list(self._bridges.values())
        return [b._thread.name for b in bridges if b.alive() and b._thread is not None]

    def _drop(self, session_id: str) -> None:
        with self._guard:
            self._map.pop(session_id, None)
            self._bridges.pop(session_id, None)

    @staticmethod
    def _resolve_final(
        session_id: str,
        result: SessionFinalEvent | SessionClosedEvent,
        bridge: _SidecarEventBridge | None,
    ) -> tuple[str, str | None]:
        http_final = result if isinstance(result, SessionFinalEvent) else None
        stream_final = bridge.final if bridge is not None else None
        if stream_final is not None and http_final is not None:
            if stream_final.text != http_final.text or stream_final.language != http_final.language:
                raise LogicError(
                    ErrorCode.internal,
                    "sidecar HTTP finish 与 SSE final 不一致，拒绝双终稿",
                    details={"http": http_final.text, "sse": stream_final.text},
                )
            return stream_final.text, stream_final.language
        if stream_final is not None:
            return stream_final.text, stream_final.language
        if http_final is not None:
            return http_final.text, http_final.language
        if isinstance(result, SessionClosedEvent) and result.reason != SessionClosedReason.finished:
            raise LogicError(ErrorCode.asr_unavailable, "sidecar finish 未返回 final")
        raise LogicError(ErrorCode.asr_unavailable, "sidecar finish 未返回 final")
