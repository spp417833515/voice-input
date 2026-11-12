"""官方引擎适配。仅在 load() 时 import qwen_asr / vLLM，import 本模块不占 GPU。"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from _contracts import (
    ASR_STREAMING_BLOCKERS,
    ErrorCode,
    LogicError,
    SessionFinalEvent,
    SessionPartialEvent,
    SttResponse,
)
from runtimes.asr.protocol import (
    DEFAULT_CHUNK_SIZE_SEC,
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEFAULT_MAX_MODEL_LEN,
    DEFAULT_MAX_SESSION_SAMPLES,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_UNFIXED_CHUNK_NUM,
    DEFAULT_UNFIXED_TOKEN_NUM,
    OFFICIAL_PARTIAL_POLICY,
    normalize_language,
)
from runtimes.asr.asr_text import parse_asr_text
from runtimes.asr.stream_step import BoundedOfficialStream, StepReport, decode_whole, numpy_pcm_ops

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_PATH = PROJECT_ROOT / ".cache/v3-feasibility/models/Qwen3-ASR-1.7B"
# 与官方 transcribe 的 MIN_ASR_INPUT_SECONDS 相同：更短的整段在尾部补零。
MIN_INPUT_S = 0.5


class AsrEngine(Protocol):
    loaded: bool

    def transcribe(self, pcm: bytes, *, language: str | None = None, context: str | None = None) -> SttResponse: ...

    def start_stream(self, session_id: str, *, language: str | None = None, context: str | None = None) -> None: ...

    def push_pcm(self, session_id: str, pcm: bytes) -> SessionPartialEvent | None: ...

    def finish_stream(self, session_id: str) -> SessionFinalEvent: ...

    def cancel_stream(self, session_id: str) -> None: ...

    def has_stream(self, session_id: str) -> bool: ...

    def drop_all_streams(self) -> None: ...


def pcm_s16le_to_float32(pcm: bytes):
    if len(pcm) % 2 != 0:
        raise LogicError(ErrorCode.invalid_request, "音频编码必须是 s16le PCM，长度为偶数")
    import numpy as np

    if not pcm:
        return np.zeros((0,), dtype=np.float32)
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


@dataclass
class _Stream:
    session_id: str
    decoder: BoundedOfficialStream | None
    samples: int = 0
    last_text: str = ""
    language: str | None = None


class OfficialEngine:
    """单模型、单官方 stream。不实现第二运行时，不跨窗拼接。

    每个 chunk 的解码由 BoundedOfficialStream 执行：官方同一算法，只给每步加语速上界并拒绝失控步。
    """

    def __init__(
        self,
        *,
        model_path: Path = DEFAULT_MODEL_PATH,
        gpu_memory_utilization: float = DEFAULT_GPU_MEMORY_UTILIZATION,
        max_model_len: int = DEFAULT_MAX_MODEL_LEN,
        chunk_size_sec: float = DEFAULT_CHUNK_SIZE_SEC,
        unfixed_chunk_num: int = DEFAULT_UNFIXED_CHUNK_NUM,
        unfixed_token_num: int = DEFAULT_UNFIXED_TOKEN_NUM,
        max_session_samples: int = DEFAULT_MAX_SESSION_SAMPLES,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self.model_path = Path(model_path)
        self.gpu_memory_utilization = gpu_memory_utilization
        self.max_model_len = max_model_len
        self.chunk_size_sec = chunk_size_sec
        self.unfixed_chunk_num = unfixed_chunk_num
        self.unfixed_token_num = unfixed_token_num
        self.max_session_samples = max_session_samples
        self.sample_rate = sample_rate
        self.loaded = False
        self.load_s: float | None = None
        self.warmup_s: float | None = None
        self.partial_policy = OFFICIAL_PARTIAL_POLICY
        self.blockers = ASR_STREAMING_BLOCKERS
        self._asr = None
        self._guard = threading.RLock()
        self._streams: dict[str, _Stream] = {}
        # 最近一次 finish 的逐步记录（单会话引擎，只留一份），供证据脚本读取。
        self.last_finished_reports: list[StepReport] = []
        self.last_transcribe_reports: list[StepReport] = []

    def load(self, *, warmup: bool = True) -> None:
        if self.loaded:
            return
        if not self.model_path.is_dir():
            raise LogicError(
                ErrorCode.asr_unavailable,
                f"官方模型目录不存在: {self.model_path}",
            )
        started = time.perf_counter()
        from qwen_asr import Qwen3ASRModel

        self._asr = Qwen3ASRModel.LLM(
            model=str(self.model_path),
            gpu_memory_utilization=self.gpu_memory_utilization,
            max_model_len=self.max_model_len,
            disable_log_stats=True,
        )
        self.loaded = True
        self.load_s = time.perf_counter() - started
        if warmup:
            warm_started = time.perf_counter()
            silence = b"\x00\x00" * int(self.sample_rate * 0.2)
            self.transcribe(silence, language="zh")
            self.warmup_s = time.perf_counter() - warm_started

    def _require(self):
        if self._asr is None or not self.loaded:
            raise LogicError(ErrorCode.asr_unavailable, "官方引擎尚未 load，拒绝假装可用")
        return self._asr

    def transcribe(self, pcm: bytes, *, language: str | None = None, context: str | None = None) -> SttResponse:
        """整段转写（手机非流式）。

        不再调用官方一次性 transcribe：它沿用 4096 token 上限，长中英混说会复读失控后被官方去复读
        折叠成只剩开头（实测 118s 剩 106 字）。改为把整段不配速地喂进与流式相同的有界内核，
        一次返回完整文本；内核拒绝失控定稿时整段显式失败，不返回截断文本。
        """
        self._require()
        samples = len(pcm) // 2
        if samples > self.max_session_samples:
            raise LogicError(
                ErrorCode.invalid_request,
                "整段音频超过 sidecar 单次识别上限",
                details={"max_session_samples": self.max_session_samples, "samples": samples},
            )
        decoder = self._new_decoder(language=language, context=context)
        try:
            decode_whole(decoder, pcm_s16le_to_float32(pcm), min_samples=int(self.sample_rate * MIN_INPUT_S))
        finally:
            self.last_transcribe_reports = list(decoder.reports)
        state = decoder.state
        return SttResponse(text=getattr(state, "text", "") or "", language=getattr(state, "language", None) or None)

    def _new_decoder(self, *, language: str | None, context: str | None) -> BoundedOfficialStream:
        """流式会话与整段转写共用的唯一内核构造：官方 init_streaming_state + 有界步。"""
        from qwen_asr.inference.utils import normalize_language_name

        state = self._require().init_streaming_state(
            context=context or "",
            language=normalize_language(language),
            unfixed_chunk_num=self.unfixed_chunk_num,
            unfixed_token_num=self.unfixed_token_num,
            chunk_size_sec=self.chunk_size_sec,
        )
        return BoundedOfficialStream(
            self._asr,
            state,
            sampling_params_factory=self._step_sampling_params,
            parse_output=lambda raw, lang: parse_asr_text(raw, lang, normalize_language_name=normalize_language_name),
            pcm=numpy_pcm_ops(),
        )

    def start_stream(self, session_id: str, *, language: str | None = None, context: str | None = None) -> None:
        self._require()
        with self._guard:
            if session_id in self._streams:
                raise LogicError(ErrorCode.conflict, "会话已存在")
            decoder = self._new_decoder(language=language, context=context)
            self._streams[session_id] = _Stream(session_id=session_id, decoder=decoder)

    @staticmethod
    def _step_sampling_params(max_tokens: int):
        # 与官方 Qwen3ASRModel.LLM 的 SamplingParams 相同（贪心），只把上限换成本步语速上界。
        from vllm import SamplingParams

        return SamplingParams(temperature=0.0, max_tokens=int(max_tokens))

    def stream_reports(self, session_id: str) -> list[StepReport]:
        with self._guard:
            stream = self._streams.get(session_id)
            if stream is None or stream.decoder is None:
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            return list(stream.decoder.reports)

    def push_pcm(self, session_id: str, pcm: bytes) -> SessionPartialEvent | None:
        self._require()
        with self._guard:
            stream = self._streams.get(session_id)
            if stream is None:
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            incoming = len(pcm) // 2
            if stream.samples + incoming > self.max_session_samples:
                raise LogicError(
                    ErrorCode.invalid_request,
                    "会话音频超过 sidecar 缓存上限，拒绝继续接收；长音频达标仍阻塞，不自动拼接",
                    details={"max_session_samples": self.max_session_samples, "samples": stream.samples + incoming},
                )
            stream.samples += incoming
            wav = pcm_s16le_to_float32(pcm)
            stream.decoder.feed(wav)
            state = stream.decoder.state
            text = getattr(state, "text", "") or ""
            language = getattr(state, "language", None) or None
            if text == stream.last_text and language == stream.language:
                return None
            stream.last_text = text
            stream.language = language
            audio_end_s = stream.samples / float(self.sample_rate)
            return SessionPartialEvent(
                session_id=session_id,
                stable_text="",
                tail_text=text,
                language=language,
                audio_end_s=audio_end_s,
            )

    def finish_stream(self, session_id: str) -> SessionFinalEvent:
        self._require()
        with self._guard:
            stream = self._streams.pop(session_id, None)
            if stream is None:
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            decoder = stream.decoder
            # 释放官方状态与累计音频引用（成功或失败都释放）
            stream.decoder = None
            try:
                decoder.finish()
            finally:
                self.last_finished_reports = list(decoder.reports)
            state = decoder.state
            text = getattr(state, "text", "") or ""
            language = getattr(state, "language", None) or None
            return SessionFinalEvent(session_id=session_id, text=text, language=language)

    def cancel_stream(self, session_id: str) -> None:
        with self._guard:
            stream = self._streams.pop(session_id, None)
            if stream is None:
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            stream.decoder = None

    def has_stream(self, session_id: str) -> bool:
        with self._guard:
            return session_id in self._streams

    def drop_all_streams(self) -> None:
        with self._guard:
            for stream in self._streams.values():
                stream.decoder = None
            self._streams.clear()

    def unload(self) -> None:
        """尽量释放会话。vLLM 进程级卸载不保证显存回到加载前，调用方应退出进程。"""
        self.drop_all_streams()
        self._asr = None
        self.loaded = False
