"""采集。按住期间把 s16le 16k 单声道 PCM 按 CHUNK_S 切块推给回调，不调用业务 API。

CHUNK_S 与 sidecar 解码步长同源（runtimes.asr.protocol.DEFAULT_CHUNK_SIZE_SEC，该模块只依赖 _contracts，
不加载 torch/vLLM）：上传节拍与解码步长不一致时，一次上传可能触发 0 或 2 步解码，字幕节拍随之抖动。

短于 0.15s 视为按键弹跳丢弃（不入业务）。120s 上限由平台看门狗触发 finish（不是 cancel）。
测试注入 FakeAudioSource，不打开真实麦克风；真实麦源在 `_linux/mic.py`。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from _focus import FocusProbe, FocusSnapshot, StaticFocusProbe
from runtimes.asr.protocol import DEFAULT_CHUNK_SIZE_SEC

SAMPLE_RATE = 16000
MIN_UTTERANCE_S = 0.15
CHUNK_S = DEFAULT_CHUNK_SIZE_SEC
BYTES_PER_SAMPLE = 2

PcmSink = Callable[[bytes], None]


class AudioSource(Protocol):
    def start(self, sink: PcmSink) -> None: ...

    def stop(self) -> None: ...


class FakeAudioSource:
    """预置 PCM，不访问声卡。stop 时一次性推出全部 PCM。"""

    def __init__(self, pcm: bytes = b"") -> None:
        self.pcm = pcm
        self.started = False
        self.stopped = False
        self._sink: PcmSink | None = None

    def start(self, sink: PcmSink) -> None:
        self.started = True
        self._sink = sink

    def push(self, pcm: bytes) -> None:
        if self._sink is None:
            raise RuntimeError("FakeAudioSource 未 start")
        self._sink(pcm)

    def stop(self) -> None:
        self.stopped = True
        if self._sink is not None and self.pcm:
            self._sink(self.pcm)


def duration_s(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> float:
    return (len(pcm) // BYTES_PER_SAMPLE) / float(sample_rate)


def chunk_width(chunk_s: float = CHUNK_S, sample_rate: int = SAMPLE_RATE) -> int:
    return int(sample_rate * chunk_s) * BYTES_PER_SAMPLE


def chunk_pcm(pcm: bytes, *, chunk_s: float = CHUNK_S, sample_rate: int = SAMPLE_RATE) -> list[bytes]:
    width = chunk_width(chunk_s, sample_rate)
    if width <= 0:
        return [pcm] if pcm else []
    return [pcm[i : i + width] for i in range(0, len(pcm), width)] if pcm else []


@dataclass
class CaptureResult:
    accepted: bool
    reason: str
    duration_s: float = 0.0
    focus: FocusSnapshot | None = None
    chunks: int = 0
    finished_by: str = "release"


ChunkSink = Callable[[int, bytes], None]


class SessionCapture:
    """一次按住说话。chunk 回调在音频线程调用，调用方只应入队，不得阻塞。"""

    def __init__(
        self,
        source: AudioSource,
        probe: FocusProbe | None = None,
        *,
        on_chunk: ChunkSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_s: float = MIN_UTTERANCE_S,
        chunk_s: float = CHUNK_S,
    ) -> None:
        self.source = source
        self.probe = probe or StaticFocusProbe()
        self.on_chunk = on_chunk
        self.clock = clock
        self.min_s = min_s
        self.width = chunk_width(chunk_s)
        self.recording = False
        self.focus: FocusSnapshot | None = None
        self._finished = False
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._total = 0
        self._next_index = 0

    def start(self) -> FocusSnapshot:
        """先绑定按下瞬间的焦点，再开麦；开麦失败抛异常，状态复位。"""
        if self.recording:
            return self.focus or FocusSnapshot(0)
        self._finished = False
        self._pending.clear()
        self._total = 0
        self._next_index = 0
        snap = getattr(self.probe, "snapshot", None)
        self.focus = snap() if callable(snap) else FocusSnapshot(self.probe.current_id(), self.probe.is_terminal())
        self.recording = True
        try:
            self.source.start(self._on_pcm)
        except Exception:
            self.recording = False
            self._finished = True
            raise
        return self.focus

    def _on_pcm(self, data: bytes) -> None:
        ready: list[tuple[int, bytes]] = []
        with self._lock:
            if self._finished:
                return  # 结束后迟到的音频回调丢弃
            self._pending.extend(data)
            self._total += len(data)
            while len(self._pending) >= self.width:
                ready.append((self._next_index, bytes(self._pending[: self.width])))
                del self._pending[: self.width]
                self._next_index += 1
        for index, chunk in ready:
            self._emit(index, chunk)

    def _emit(self, index: int, chunk: bytes) -> None:
        if self.on_chunk is not None:
            self.on_chunk(index, chunk)

    def finish(self, *, reason: str = "release") -> CaptureResult:
        if self._finished:
            return CaptureResult(False, "already_finished", focus=self.focus, finished_by=reason)
        self.recording = False
        try:
            self.source.stop()  # stop 期间推出的尾部音频仍计入
        finally:
            self._finished = True
        with self._lock:
            lasted = self._total / BYTES_PER_SAMPLE / float(SAMPLE_RATE)
            tail = bytes(self._pending)
            self._pending.clear()
            index = self._next_index
        if lasted < self.min_s:
            return CaptureResult(False, "too_short", duration_s=lasted, focus=self.focus, finished_by=reason)
        if tail:
            self._emit(index, tail)
            index += 1
        return CaptureResult(True, "ok", duration_s=lasted, focus=self.focus, chunks=index, finished_by=reason)

    def cancel(self) -> None:
        self.recording = False
        self._finished = True
        self.source.stop()
