"""官方流式解码一步的受控实现（只在已加载的官方 Qwen3ASRModel 上运行）。

算法与 qwen_asr 0.0.6 `Qwen3ASRModel.streaming_transcribe` / `finish_streaming_transcribe`
逐行一致：同一 `init_streaming_state` 产生的状态与 prompt_raw、同一 `audio_accum` 全量重喂、
同一 unfixed_chunk/unfixed_token 前缀回退。文本解析由调用方注入（生产为 `asr_text.parse_asr_text`：
官方 parse_asr_output 的语言/文本边界，不做阈值 20 的重复折叠）。解码只改三点：

1. 每一步的 max_tokens 取「这一步最多可能新增多少文字」的语速上界，而不是官方整段转写用的
   4096。官方没有按音频长度的生成上限，模型在长前缀上进入复读时会一直生成到 4096 token
   （实测 85s 处单步 23.1s；整段 transcribe 在 118s 中英混说上同样 4096 token 后丢掉 90% 内容）。
2. 一步若被上限截断（vLLM finish_reason == "length"），这一步的续写不写入 `_raw_decoded`。
   音频已进入 `audio_accum`，下一步连同新音频整体重识别，不丢音频。字幕步的上界固定，
   保证失控时每步耗时仍小于一个 chunk，字幕停在最后一次可信结果。
3. 定稿时若还有未覆盖的音频（被拒步），即使没有尾音频也再解码一次，上界按「文字最后一次
   增长以来的音频时长」放宽；仍被截断则响亮失败，绝不把旧字幕当成完整定稿。

数组运算与文本解析由调用方注入（生产用 numpy 与 asr_text.parse_asr_text），
本模块因此可在没有 numpy / qwen_asr 的闸门环境里做纯算法测试。
不做跨窗拼接、不按字符串删重、不臆造 stable/时间戳。
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SAMPLE_RATE = 16000
# 语速上界：每秒新增 token 数。中文约 1 token/字，快语速 ~7 字/s；英文 ~3.5 词/s ≈ 5 token/s。
# 取 12 留出标点与数字；超过即视为该步失控。
MAX_TOKENS_PER_AUDIO_S = 12.0
# 模型可暂时滞后于音频（等词说完再出字），上界额外容纳这么多秒的未出字音频。
# 字幕步上界 = 回退 + 余量 16 + ceil(12*(chunk_size_sec+2))，chunk_size_sec 取自 state（不硬编码）：
# 默认 0.4s 时为 5+16+29 = 50 token（旧 0.5s 时 51）。实测 3 段 118s 自然语料单步最多 13 token。
# 本机 1.7B 解码约 180 token/s：失控步被截在 ~50 token 时连同 120s 预填约 0.4s
# （r2 用 3s 余量即 63 token 时，失控步最长 0.51s，会缓慢积压）。
LAG_ALLOWANCE_S = 2.0
# "language Chinese<asr_text>" 一类元信息与标点的余量。
SLACK_TOKENS = 16

ParseOutput = Callable[[str, "str | None"], "tuple[str, str]"]


@dataclass(frozen=True)
class PcmOps:
    """一维 float PCM 的运算；生产实现见 numpy_pcm_ops()。切片用对象自身的 [a:b]。"""

    zeros: Callable[[int], Any]
    concat: Callable[[Any, Any], Any]
    size: Callable[[Any], int]


def numpy_pcm_ops() -> PcmOps:
    import numpy as np

    return PcmOps(
        zeros=lambda n: np.zeros((int(n),), dtype=np.float32),
        concat=lambda a, b: b if a.shape[0] == 0 else np.concatenate([a, b], axis=0),
        size=lambda a: int(a.shape[0]),
    )


@dataclass(frozen=True)
class StepReport:
    """一次模型调用的事实记录，供证据与测试使用，不进入产品协议。"""

    audio_s: float
    prefix_tokens: int
    rollback_tokens: int
    max_tokens: int
    gen_tokens: int
    prompt_tokens: int
    finish_reason: str
    accepted: bool
    final: bool
    dt_s: float


class BoundedOfficialStream:
    """包装一个官方 ASRStreamingState；同一时刻只能由一个线程调用。"""

    def __init__(
        self,
        asr,
        state,
        *,
        sampling_params_factory: Callable[[int], Any],
        parse_output: ParseOutput,
        pcm: PcmOps,
    ) -> None:
        self._asr = asr
        self.state = state
        self._make_params = sampling_params_factory
        self._parse = parse_output
        self._pcm = pcm
        self.rejected_streak = 0
        # 文字最后一次增长以来经过的 chunk 数（被拒或只重写回退部分都算未增长）。
        self.stalled_chunks = 0
        self.reports: list[StepReport] = []

    @property
    def audio_s(self) -> float:
        return self._pcm.size(self.state.audio_accum) / float(SAMPLE_RATE)

    def feed(self, wav) -> bool:
        """追加 float PCM；每凑满一个 chunk 解码一步。返回本次是否至少接受了一步。"""
        state, pcm = self.state, self._pcm
        if pcm.size(wav) > 0:
            state.buffer = pcm.concat(state.buffer, wav)
        accepted_any = False
        while pcm.size(state.buffer) >= state.chunk_size_samples:
            chunk = state.buffer[: state.chunk_size_samples]
            state.buffer = state.buffer[state.chunk_size_samples :]
            state.audio_accum = pcm.concat(state.audio_accum, chunk)
            prefix, rollback = self._step_prefix()
            accepted_any |= self._decode(prefix, rollback, final=False)
        return accepted_any

    def finish(self) -> None:
        """官方 finish：冲刷不足一个 chunk 的尾音频再解码一次。

        官方在无尾音频时原样返回；这里在还有被拒步未覆盖的音频时也必须再解码，否则定稿会
        停在旧字幕而丢掉其后的音频。
        """
        state, pcm = self.state, self._pcm
        tail_n = pcm.size(state.buffer) if state.buffer is not None else 0
        if tail_n == 0 and self.rejected_streak == 0:
            return
        if tail_n:
            state.audio_accum = pcm.concat(state.audio_accum, state.buffer)
            state.buffer = pcm.zeros(0)
        prefix, rollback = self._finish_prefix()
        pending_s = tail_n / float(SAMPLE_RATE) + state.chunk_size_sec * self.stalled_chunks
        if not self._decode(prefix, rollback, final=True, pending_s=pending_s):
            from _contracts import ErrorCode, LogicError

            raise LogicError(
                ErrorCode.asr_unavailable,
                "定稿解码超过语速上界（模型复读失控），拒绝把截断结果当定稿",
                details={"audio_s": round(self.audio_s, 3), "max_tokens": self.reports[-1].max_tokens},
            )

    def _tokenizer(self):
        return self._asr.processor.tokenizer

    def _step_prefix(self) -> tuple[str, int]:
        # 与官方 streaming_transcribe 相同：前 unfixed_chunk_num 步不带前缀；之后回退 K 个 token，
        # 回退后若切在多字节字符中间（出现 U+FFFD）则多回退一个。
        state = self.state
        if state.chunk_id < state.unfixed_chunk_num:
            return "", 0
        tok = self._tokenizer()
        cur_ids = tok.encode(state._raw_decoded)
        k = int(state.unfixed_token_num)
        while True:
            end_idx = max(0, len(cur_ids) - k)
            prefix = tok.decode(cur_ids[:end_idx]) if end_idx > 0 else ""
            if "\ufffd" not in prefix:
                return prefix, len(cur_ids) - end_idx
            if end_idx == 0:
                return "", len(cur_ids)
            k += 1

    def _finish_prefix(self) -> tuple[str, int]:
        # 与官方 finish_streaming_transcribe 相同（官方此处无 U+FFFD 循环，至少保留 1 个 token）。
        state = self.state
        if state.chunk_id < state.unfixed_chunk_num:
            return "", 0
        tok = self._tokenizer()
        cur_ids = tok.encode(state._raw_decoded)
        end_idx = max(1, len(cur_ids) - int(state.unfixed_token_num))
        return tok.decode(cur_ids[:end_idx]), len(cur_ids) - end_idx

    def max_tokens_for(self, prefix: str, rollback: int, *, pending_s: float | None = None) -> int:
        """这一步续写的语速上界。

        无前缀时要重写全部音频；有前缀时只需覆盖回退的 token 与尚未出字的音频：
        字幕步按一个 chunk + 滞后余量；定稿步按尾音频 + 文字停滞以来的音频 + 滞后余量。
        """
        if not prefix:
            uncovered_s = self.audio_s
        elif pending_s is None:
            uncovered_s = self.state.chunk_size_sec + LAG_ALLOWANCE_S
        else:
            uncovered_s = pending_s + LAG_ALLOWANCE_S
        return rollback + SLACK_TOKENS + int(math.ceil(MAX_TOKENS_PER_AUDIO_S * uncovered_s))

    def _decode(self, prefix: str, rollback: int, *, final: bool, pending_s: float | None = None) -> bool:
        state = self.state
        max_tokens = self.max_tokens_for(prefix, rollback, pending_s=pending_s)
        inp = {"prompt": state.prompt_raw + prefix, "multi_modal_data": {"audio": [state.audio_accum]}}
        started = time.perf_counter()
        outputs = self._asr.model.generate([inp], sampling_params=self._make_params(max_tokens), use_tqdm=False)
        dt = time.perf_counter() - started
        out = outputs[0].outputs[0]
        finish_reason = str(out.finish_reason)
        accepted = finish_reason != "length"
        self.reports.append(
            StepReport(
                audio_s=self.audio_s,
                prefix_tokens=len(self._tokenizer().encode(prefix)) if prefix else 0,
                rollback_tokens=rollback,
                max_tokens=max_tokens,
                gen_tokens=len(out.token_ids),
                prompt_tokens=len(outputs[0].prompt_token_ids or ()),
                finish_reason=finish_reason,
                accepted=accepted,
                final=final,
                dt_s=dt,
            )
        )
        state.chunk_id += 1
        if not accepted:
            self.rejected_streak += 1
            self.stalled_chunks += 1
            return False
        self.rejected_streak = 0
        self.stalled_chunks = 0 if len(out.token_ids) > rollback else self.stalled_chunks + 1
        state._raw_decoded = prefix + out.text
        state.language, state.text = self._parse(state._raw_decoded, state.force_language)
        return True


def decode_whole(stream: BoundedOfficialStream, wav, *, min_samples: int) -> BoundedOfficialStream:
    """整段音频不配速地走同一有界流内核：一次喂完再定稿。

    短于 min_samples 的音频按官方 transcribe 的做法在尾部补零（官方 MIN_ASR_INPUT_SECONDS=0.5）。
    """
    pcm = stream._pcm
    missing = min_samples - pcm.size(wav)
    if missing > 0:
        wav = pcm.concat(wav, pcm.zeros(missing))
    stream.feed(wav)
    stream.finish()
    return stream
