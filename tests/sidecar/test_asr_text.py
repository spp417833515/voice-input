"""asr_text.parse_asr_text 的纯算法测试（无 qwen_asr，常规 sidecar 闸门运行）。

只修「解析器人工删文本」：真实连续 ≥20 次的短句原样保留；语言/文本边界与官方相同；
失控仍由有界内核按 finish_reason == "length" 拒绝并在定稿时 503，与解析无关。
与官方 parse_asr_output 的逐分支等价见 test_stream_step_official.py（venv-asr）。
"""
from __future__ import annotations

import pytest

from _contracts import ErrorCode, LogicError
from runtimes.asr.asr_text import ASR_TEXT_TAG, parse_asr_text
from tests.sidecar.test_stream_step import SR, FakeLLM, FakeAsr, LIST_PCM, _chunks, _speech, _state
from runtimes.asr.stream_step import BoundedOfficialStream, decode_whole


def _title(v: str) -> str:
    # 与官方 normalize_language_name 同规则的测试替身（生产注入官方函数）
    return v[:1].upper() + v[1:].lower()


def _p(raw, lang=None):
    return parse_asr_text(raw, lang, normalize_language_name=_title)


def test_language_and_text_boundaries():
    assert _p(None) == ("", "")
    assert _p("   ") == ("", "")
    assert _p("纯文本，没有标签。") == ("", "纯文本，没有标签。")
    assert _p("language chinese<asr_text>你好。") == ("Chinese", "你好。")
    assert _p("language English\nextra\n<asr_text> Hello. ") == ("English", "Hello.")
    assert _p("language None<asr_text>") == ("", "")
    assert _p("language None<asr_text>还有字") == ("", "还有字")
    assert _p("meta<asr_text>文本") == ("", "文本")
    # 强制语言：整段视为纯文本（含标签也原样，与官方一致）
    assert _p("你好。", "Chinese") == ("Chinese", "你好。")
    assert _p(f"language English{ASR_TEXT_TAG}hi", "Chinese") == ("Chinese", f"language English{ASR_TEXT_TAG}hi")


@pytest.mark.parametrize(
    "text",
    [
        "好的，" * 25,
        "对" * 30,
        "我再说一遍九点开会。" * 21,
        "testing, " * 22,
    ],
)
def test_real_20x_repeats_are_kept_verbatim(text):
    assert _p(text, "Chinese")[1] == text.strip()
    assert _p(f"language Chinese{ASR_TEXT_TAG}{text}")[1] == text.strip()


def _stream(llm, **kw):
    return BoundedOfficialStream(
        FakeAsr(llm),
        _state(**kw),
        sampling_params_factory=lambda n: n,
        parse_output=lambda raw, lang: parse_asr_text(raw, lang, normalize_language_name=_title),
        pcm=LIST_PCM,
    )


def test_kernel_keeps_25x_spoken_repeat_through_partials_and_final():
    pieces = ["好的，"] * 25 + ["结束。"]
    llm = FakeLLM(_speech(pieces))
    s = _stream(llm)
    for c in _chunks(len(pieces)):
        s.feed(c)
    s.finish()
    assert s.state.text == "好的，" * 25 + "结束。"
    assert all(r.accepted for r in s.reports)
    assert max(r.max_tokens for r in s.reports if r.prefix_tokens) < 4096


def test_runaway_still_rejected_and_final_is_explicit_503_with_new_parser():
    pieces = ["甲乙", "丙丁", "戊己", "庚辛", "壬癸", "子丑"]
    llm = FakeLLM(_speech(pieces), runaway_at={n * SR // 2 for n in range(4, 7)})
    s = _stream(llm)
    with pytest.raises(LogicError) as err:
        decode_whole(s, [0.0] * (3 * SR), min_samples=SR // 2)
    assert err.value.code == ErrorCode.asr_unavailable
    rejected = [r for r in s.reports if not r.accepted]
    assert rejected and all(r.finish_reason == "length" and r.max_tokens < 4096 for r in rejected)
    # 失控续写没有写入可见文本
    assert "复" not in s.state.text
