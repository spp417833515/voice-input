"""BoundedOfficialStream 的纯算法测试：假 vLLM、字符级 tokenizer、列表 PCM、恒等解析。

不需要 numpy / qwen_asr / GPU，常规 sidecar 闸门即可运行。官方解析与 numpy 的部分见
test_stream_step_official.py（只在 venv-asr 显式运行）。
覆盖：与官方算法一致的前缀回退、语速上界、失控步不写入历史、积压时字幕冻结且定稿补回、
定稿失控时响亮失败、正当重复不被删除、整段不配速解码与短音频补零。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from _contracts import LogicError
from runtimes.asr.stream_step import (
    LAG_ALLOWANCE_S,
    MAX_TOKENS_PER_AUDIO_S,
    SLACK_TOKENS,
    BoundedOfficialStream,
    PcmOps,
    decode_whole,
)

LIST_PCM = PcmOps(zeros=lambda n: [0.0] * int(n), concat=lambda a, b: a + b, size=len)

SR = 16000


class CharTokenizer:
    def encode(self, text: str) -> list[int]:
        return [ord(c) for c in text]

    def decode(self, ids) -> str:
        return "".join(chr(i) for i in ids)


@dataclass
class _Out:
    text: str
    token_ids: list[int]
    finish_reason: str


@dataclass
class _ReqOut:
    outputs: list
    prompt_token_ids: list[int]


@dataclass
class FakeLLM:
    """每次 generate 按脚本返回「全量真文本」中尚未在 prompt 前缀里的部分。"""

    full_text_at: callable
    runaway_at: set = field(default_factory=set)
    calls: list = field(default_factory=list)

    def generate(self, inputs, sampling_params, use_tqdm=False):
        (inp,) = inputs
        audio = inp["multi_modal_data"]["audio"][0]
        prefix = inp["prompt"][len("P:"):]
        n = len(audio)
        self.calls.append({"prefix": prefix, "samples": n, "max_tokens": sampling_params})
        if n in self.runaway_at:
            text = "复" * sampling_params
            return [_ReqOut([_Out(text, [1] * sampling_params, "length")], [0] * (n // 640))]
        truth = self.full_text_at(n)
        assert truth.startswith(prefix), (truth, prefix)
        cont = truth[len(prefix):]
        if len(cont) > sampling_params:
            return [_ReqOut([_Out(cont[:sampling_params], [1] * sampling_params, "length")], [0])]
        return [_ReqOut([_Out(cont, [1] * len(cont), "stop")], [0])]


class FakeAsr:
    def __init__(self, llm):
        self.model = llm
        self.processor = type("P", (), {"tokenizer": CharTokenizer()})()


def _state(chunk_s=0.5, unfixed_chunk=2, unfixed_token=3, force_language="Chinese"):
    # 字段与官方 ASRStreamingState 相同。
    return SimpleNamespace(
        unfixed_chunk_num=unfixed_chunk,
        unfixed_token_num=unfixed_token,
        chunk_size_sec=chunk_s,
        chunk_size_samples=int(chunk_s * SR),
        chunk_id=0,
        buffer=[],
        audio_accum=[],
        prompt_raw="P:",
        context="",
        force_language=force_language,
        language="",
        text="",
        _raw_decoded="",
    )


def _stream(llm, **kw):
    return BoundedOfficialStream(
        FakeAsr(llm),
        _state(**kw),
        sampling_params_factory=lambda n: n,
        parse_output=lambda raw, lang: (lang or "", raw),
        pcm=LIST_PCM,
    )


def _speech(text_per_chunk: list[str]):
    """第 i 个 0.5s chunk 结束时，真文本是前 i 段的拼接。"""

    def at(samples: int) -> str:
        i = -(-samples // (SR // 2))
        return "".join(text_per_chunk[:i])

    return at


def _chunks(n, seconds=0.5):
    return [[0.0] * int(seconds * SR) for _ in range(n)]


def test_prefix_rollback_matches_official_algorithm():
    pieces = ["今天", "我们", "开会。", "好的，", "好的。"]
    llm = FakeLLM(_speech(pieces))
    s = _stream(llm)
    for c in _chunks(5):
        s.feed(c)
    # 前 2 步无前缀；之后前缀 = 上一步 raw 回退 3 个 token
    assert [c["prefix"] for c in llm.calls[:2]] == ["", ""]
    assert llm.calls[2]["prefix"] == "今天我们"[:-3]
    assert llm.calls[4]["prefix"] == "今天我们开会。好的，"[:-3]
    # 正当重复「好的，好的。」原样保留，没有任何删重
    assert s.state.text == "今天我们开会。好的，好的。"
    assert all(r.accepted for r in s.reports)


def test_budget_without_prefix_covers_all_audio_and_with_prefix_is_local():
    llm = FakeLLM(_speech(["甲乙", "丙丁", "戊己", "庚辛"]))
    s = _stream(llm)
    s.feed(_chunks(1)[0])
    assert s.reports[0].max_tokens == SLACK_TOKENS + int(math.ceil(MAX_TOKENS_PER_AUDIO_S * 0.5))
    for c in _chunks(3):
        s.feed(c)
    r = s.reports[-1]
    assert r.prefix_tokens == 3 and r.rollback_tokens == 3
    assert r.max_tokens == 3 + SLACK_TOKENS + int(math.ceil(MAX_TOKENS_PER_AUDIO_S * (0.5 + LAG_ALLOWANCE_S)))


def test_runaway_step_is_rejected_not_written_and_next_step_recovers():
    pieces = ["甲乙", "丙丁", "戊己", "庚辛", "壬癸", "子丑"]
    llm = FakeLLM(_speech(pieces), runaway_at={4 * SR // 2})
    s = _stream(llm)
    texts = []
    for c in _chunks(6):
        s.feed(c)
        texts.append(s.state.text)
    assert s.reports[3].finish_reason == "length" and not s.reports[3].accepted
    assert texts[3] == texts[2]  # 失控步不改变已有文字
    assert "复" not in s.state._raw_decoded
    assert s.state.text == "".join(pieces)  # 下一步整体重识别，音频不丢
    assert s.rejected_streak == 0


def test_backlog_beyond_step_budget_freezes_captions_then_finish_recovers_all():
    # 模型滞后 4s 后一次性吐出 60 个不同字（≈12 字/s，未超语速上界），超过固定的字幕步上界
    burst = "".join(chr(0x4E00 + i) for i in range(60))
    pieces = ["开始", "了吗", "是的"] + [""] * 8 + [burst, "完"]
    llm = FakeLLM(_speech(pieces))
    s = _stream(llm)
    texts = []
    for c in _chunks(len(pieces)):
        s.feed(c)
        texts.append(s.state.text)
    rejected = [r for r in s.reports if not r.accepted]
    assert rejected and all(r.max_tokens == rejected[0].max_tokens for r in rejected), "字幕步上界固定，不随被拒累加"
    assert texts[-1] == texts[10] == "开始了吗是的"  # 字幕停在最后可信结果，不出现截断的半截
    s.finish()  # 无尾音频，但有被拒步未覆盖的音频：必须再解码
    assert s.reports[-1].final and s.reports[-1].accepted
    assert s.state.text == "开始了吗是的" + burst + "完"


def test_runaway_through_finish_fails_loudly_even_without_tail():
    pieces = ["甲乙", "丙丁", "戊己", "庚辛"]
    llm = FakeLLM(_speech(pieces), runaway_at={4 * SR // 2})
    s = _stream(llm)
    for c in _chunks(4):
        s.feed(c)
    assert s.rejected_streak == 1
    with pytest.raises(LogicError):
        s.finish()


def test_finish_flushes_tail_and_runaway_finish_fails_loudly():
    pieces = ["你好", "世界", "再见", "朋友"]
    llm = FakeLLM(_speech(pieces))
    s = _stream(llm)
    for c in _chunks(3):
        s.feed(c)
    s.feed([0.0] * (SR // 4))
    s.finish()
    assert s.reports[-1].final and s.state.text == "".join(pieces)

    llm2 = FakeLLM(_speech(pieces), runaway_at={3 * SR // 2 + SR // 4})
    s2 = _stream(llm2)
    for c in _chunks(3):
        s2.feed(c)
    s2.feed([0.0] * (SR // 4))
    with pytest.raises(LogicError):
        s2.finish()


def test_finish_without_tail_keeps_state():
    llm = FakeLLM(_speech(["一二", "三四"]))
    s = _stream(llm)
    for c in _chunks(2):
        s.feed(c)
    n = len(llm.calls)
    s.finish()
    assert len(llm.calls) == n and s.state.text == "一二三四"


def test_decode_whole_unpaced_equals_paced_and_keeps_legit_repeats():
    pieces = ["好的，", "好的。", "我再说一遍：", "九点开会。", "我再说一遍：", "九点开会。"]
    paced = _stream(FakeLLM(_speech(pieces)))
    for c in _chunks(len(pieces)):
        paced.feed(c)
    paced.finish()
    whole = decode_whole(_stream(FakeLLM(_speech(pieces))), [0.0] * (SR // 2 * len(pieces)), min_samples=SR // 2)
    assert whole.state.text == paced.state.text == "".join(pieces)


def test_decode_whole_pads_short_audio_like_official_and_tail_is_flushed():
    llm = FakeLLM(_speech(["嗯。"]))
    s = decode_whole(_stream(llm), [0.0] * (SR // 10), min_samples=SR // 2)
    assert llm.calls[0]["samples"] == SR // 2 and s.state.text == "嗯。"
    llm2 = FakeLLM(_speech(["一", "二", "三"]))
    s2 = decode_whole(_stream(llm2), [0.0] * (SR + SR // 4), min_samples=SR // 2)
    assert llm2.calls[-1]["samples"] == SR + SR // 4 and s2.reports[-1].final and s2.state.text == "一二三"


def test_decode_whole_runaway_fails_loudly_instead_of_returning_truncated_text():
    pieces = ["甲乙", "丙丁", "戊己", "庚辛", "壬癸"]
    llm = FakeLLM(_speech(pieces), runaway_at={n * SR // 2 for n in range(3, 6)})
    with pytest.raises(LogicError):
        decode_whole(_stream(llm), [0.0] * (SR // 2 * 5), min_samples=SR // 2)


def test_silence_produces_no_text():
    llm = FakeLLM(lambda n: "")
    s = decode_whole(_stream(llm), [0.0] * (SR * 3), min_samples=SR // 2)
    assert s.state.text == "" and all(r.accepted for r in s.reports)
