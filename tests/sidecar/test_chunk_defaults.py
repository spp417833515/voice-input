"""默认步长 0.4/5/5 的单一来源与预算（纯算法，常规闸门运行，不需要 numpy/qwen_asr/GPU）。

覆盖：protocol 默认值、桌面上传节拍与解码步长同源、上界按 state 实际步长计算（不是 0.5 时代的 51）、
上传节拍一致时每块恰好一步、旧 0.5 节拍会出现 0/2 步、末块不对齐与 120s 尾音频不丢、积压后定稿补回。
"""
from __future__ import annotations

import math

import _capture
from _capture import FakeAudioSource, SessionCapture, chunk_width
from runtimes.asr import protocol
from runtimes.asr.protocol import (
    DEFAULT_CHUNK_SIZE_SEC,
    DEFAULT_MAX_SESSION_SAMPLES,
    DEFAULT_UNFIXED_CHUNK_NUM,
    DEFAULT_UNFIXED_TOKEN_NUM,
)
from runtimes.asr.stream_step import LAG_ALLOWANCE_S, MAX_TOKENS_PER_AUDIO_S, SLACK_TOKENS, BoundedOfficialStream
from tests.sidecar.test_stream_step import LIST_PCM, SR, FakeAsr, FakeLLM, _state

STEP = int(round(DEFAULT_CHUNK_SIZE_SEC * SR))


def _default_stream(llm):
    return BoundedOfficialStream(
        FakeAsr(llm),
        _state(chunk_s=DEFAULT_CHUNK_SIZE_SEC, unfixed_chunk=DEFAULT_UNFIXED_CHUNK_NUM, unfixed_token=DEFAULT_UNFIXED_TOKEN_NUM),
        sampling_params_factory=lambda n: n,
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )


def _speech_per_step(pieces):
    """第 i 个默认步长结束时真文本是前 i 段；超出 pieces 后保持全文。"""
    return lambda samples: "".join(pieces[: -(-samples // STEP)])


def test_protocol_defaults_and_unfixed_window_unchanged_at_2s():
    assert (DEFAULT_CHUNK_SIZE_SEC, DEFAULT_UNFIXED_CHUNK_NUM, DEFAULT_UNFIXED_TOKEN_NUM) == (0.4, 5, 5)
    assert math.isclose(DEFAULT_CHUNK_SIZE_SEC * DEFAULT_UNFIXED_CHUNK_NUM, 2.0)
    assert STEP == 6400 and DEFAULT_MAX_SESSION_SAMPLES % STEP == 0


def test_desktop_upload_cadence_is_the_same_constant():
    assert _capture.CHUNK_S is protocol.DEFAULT_CHUNK_SIZE_SEC
    assert chunk_width() == STEP * 2


def test_step_budget_is_computed_from_state_chunk_not_hardcoded_51():
    pieces = ["甲乙丙", "丁戊己", "庚辛壬", "癸子丑", "寅卯辰", "巳午未", "申酉戌"]
    s = _default_stream(FakeLLM(_speech_per_step(pieces)))
    s.feed([0.0] * STEP)
    assert s.reports[0].max_tokens == SLACK_TOKENS + int(math.ceil(MAX_TOKENS_PER_AUDIO_S * DEFAULT_CHUNK_SIZE_SEC))
    for _ in range(6):
        s.feed([0.0] * STEP)
    r = s.reports[-1]
    assert r.rollback_tokens == DEFAULT_UNFIXED_TOKEN_NUM
    assert r.max_tokens == 5 + 16 + 29 == 50
    assert s.state.text == "".join(pieces)


def test_capture_chunks_from_arbitrary_device_blocks_give_one_decode_step_each():
    pieces = [f"第{i}句。" for i in range(10)]
    s = _default_stream(FakeLLM(_speech_per_step(pieces)))
    steps_per_chunk = []

    def on_chunk(_index, pcm):
        before = len(s.reports)
        s.feed([0.0] * (len(pcm) // 2))
        steps_per_chunk.append(len(s.reports) - before)

    src = FakeAudioSource()
    cap = SessionCapture(src, on_chunk=on_chunk, clock=lambda: 0.0)
    cap.start()
    block = b"\x00\x00" * 1700  # 声卡回调块大小与步长无关
    for _ in range(10 * STEP // 1700 + 1):
        src.push(block)
    assert steps_per_chunk and set(steps_per_chunk) == {1}


def test_old_half_second_cadence_against_0_4_decoder_gives_0_or_2_steps_per_upload():
    # 证明上传节拍必须与解码步长同源：不同源时字幕节拍会成对抖动
    s = _default_stream(FakeLLM(_speech_per_step(["x"] * 20)))
    counts = []
    for _ in range(8):
        before = len(s.reports)
        s.feed([0.0] * (SR // 2))
        counts.append(len(s.reports) - before)
    assert 2 in counts and sum(counts) == 8 * (SR // 2) // STEP


def test_misaligned_tail_and_full_120s_are_not_dropped():
    total = DEFAULT_MAX_SESSION_SAMPLES - SR // 10  # 119.9s：末块 0.3s 不对齐
    n_steps = total // STEP
    pieces = [f"{i}," for i in range(n_steps + 1)]
    s = _default_stream(FakeLLM(_speech_per_step(pieces)))
    for pos in range(0, total, SR // 2):  # 外部任意块大小
        s.feed([0.0] * min(SR // 2, total - pos))
    assert len(s.reports) == n_steps == 299
    s.finish()
    assert s.reports[-1].final and s.state.text == "".join(pieces)
    assert len(s.state.audio_accum) == total


def test_backlog_then_finish_budget_uses_default_step():
    burst = "".join(chr(0x4E00 + i) for i in range(60))
    pieces = ["开始", "了吗", "是的"] + [""] * 10 + [burst, "完"]
    s = _default_stream(FakeLLM(_speech_per_step(pieces)))
    for _ in pieces:
        s.feed([0.0] * STEP)
    rejected = [r for r in s.reports if not r.accepted]
    assert rejected and all(r.max_tokens == rejected[0].max_tokens for r in rejected)
    assert rejected[0].max_tokens == rejected[0].rollback_tokens + SLACK_TOKENS + int(
        math.ceil(MAX_TOKENS_PER_AUDIO_S * (DEFAULT_CHUNK_SIZE_SEC + LAG_ALLOWANCE_S))
    )
    s.finish()
    assert s.reports[-1].final and s.state.text == "开始了吗是的" + burst + "完"
