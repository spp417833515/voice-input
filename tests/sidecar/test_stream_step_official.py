"""有界流内核接上官方 parse_asr_output / ASRStreamingState / numpy / vLLM SamplingParams 的部分，
以及 OfficialEngine.transcribe 走同一内核。假模型，不占 GPU。

只在隔离 venv-asr 显式运行：
  .cache/v3-feasibility/venv-asr/bin/python -m pytest -p no:cacheprovider tests/sidecar/test_stream_step_official.py
常规闸门环境没有 numpy / qwen_asr，本文件整体跳过；纯算法部分见 test_stream_step.py。
（不能与其它 sidecar 测试同进程跑：其它测试断言应用核心未导入 torch。）
"""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("qwen_asr")
pytest.importorskip("vllm")

from qwen_asr.inference.qwen3_asr import ASRStreamingState  # noqa: E402
from qwen_asr.inference import utils as official_utils  # noqa: E402
from qwen_asr.inference.utils import normalize_language_name, parse_asr_output  # noqa: E402
from runtimes.asr.asr_text import ASR_TEXT_TAG, LANG_PREFIX, parse_asr_text  # noqa: E402

from _contracts import ErrorCode, LogicError  # noqa: E402
from runtimes.asr.engine import OfficialEngine  # noqa: E402
from runtimes.asr.protocol import DEFAULT_CHUNK_SIZE_SEC  # noqa: E402
from runtimes.asr.stream_step import BoundedOfficialStream, numpy_pcm_ops  # noqa: E402
from tests.sidecar.test_stream_step import CharTokenizer, FakeLLM, _speech  # noqa: E402

SR = 16000


class _Budget(FakeLLM):
    def generate(self, inputs, sampling_params, use_tqdm=False):
        return super().generate(inputs, getattr(sampling_params, "max_tokens", sampling_params), use_tqdm)


class FakeOfficialAsr:
    def __init__(self, llm):
        self.model = llm
        self.processor = type("P", (), {"tokenizer": CharTokenizer()})()
        self.init_calls = []
        self.init_params = []

    def init_streaming_state(self, *, context, language, unfixed_chunk_num, unfixed_token_num, chunk_size_sec):
        self.init_calls.append({"context": context, "language": language, "chunk_size_sec": chunk_size_sec})
        self.init_params.append((chunk_size_sec, unfixed_chunk_num, unfixed_token_num))
        return ASRStreamingState(
            unfixed_chunk_num=unfixed_chunk_num,
            unfixed_token_num=unfixed_token_num,
            chunk_size_sec=chunk_size_sec,
            chunk_size_samples=int(round(chunk_size_sec * SR)),
            chunk_id=0,
            buffer=np.zeros((0,), dtype=np.float32),
            audio_accum=np.zeros((0,), dtype=np.float32),
            prompt_raw="P:",
            context=context,
            force_language=language,
            language="",
            text="",
            _raw_decoded="",
        )


def _engine(llm) -> tuple[OfficialEngine, FakeOfficialAsr]:
    eng = OfficialEngine()
    fake = FakeOfficialAsr(llm)
    eng._asr = fake
    eng.loaded = True
    return eng, fake


def _pcm(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * SR)


def test_official_parse_folds_20x_runs_which_production_parser_does_not():
    # 官方 parse_asr_output 的阈值 20 折叠会删掉真实连说 ≥20 次的短句；生产改用 asr_text.parse_asr_text
    assert parse_asr_output("对，对，对。我再说一遍：九点。我再说一遍：九点。", user_language="Chinese")[1] == "对，对，对。我再说一遍：九点。我再说一遍：九点。"
    assert parse_asr_output("好的，" * 25, user_language="Chinese")[1] == "好的，"
    assert _prod_parse("好的，" * 25, "Chinese")[1] == "好的，" * 25


def _prod_parse(raw, lang):
    return parse_asr_text(raw, lang, normalize_language_name=normalize_language_name)


def test_production_parser_constants_match_official():
    assert ASR_TEXT_TAG == official_utils._ASR_TEXT_TAG
    assert LANG_PREFIX == official_utils._LANG_PREFIX


@pytest.mark.parametrize("lang", [None, "Chinese", "English"])
@pytest.mark.parametrize(
    "raw",
    [
        None, "", "  ", "纯文本。", "language Chinese<asr_text>你好。", "language chinese<asr_text> 好的，好的。 ",
        "language English\nmeta\n<asr_text>Hello, hello.", "language None<asr_text>", "language None<asr_text>残留",
        "meta only<asr_text>文本", "<asr_text>", "language <asr_text>无语言名", "Language JAPANESE<asr_text>こんにちは",
        "对，对，对。我再说一遍：九点。我再说一遍：九点。", "好的，" * 19, "对" * 20,
        "甚至出现交易几乎停滞的情况。嗯哼，哦， yeah yeah， he wasn't even that big.",
    ],
)
def test_production_parser_equals_official_when_no_20x_run(raw, lang):
    assert _prod_parse(raw, lang) == parse_asr_output(raw, user_language=lang)


def test_engine_decoder_uses_production_parser_and_keeps_25x_repeat():
    pieces = ["好的，"] * 25 + ["结束。"]
    eng, _ = _engine(_Budget(_speech(pieces)))
    out = eng.transcribe(_pcm(0.5 * len(pieces)), language="zh")
    assert out.text == "好的，" * 25 + "结束。"


def test_numpy_kernel_with_official_state_matches_char_reference():
    pieces = ["甚至出现", "交易几乎", "停滞的", "情况。", "Yeah, ", "yeah."]
    llm = _Budget(_speech(pieces))
    asr = FakeOfficialAsr(llm)
    state = asr.init_streaming_state(context="", language=None, unfixed_chunk_num=4, unfixed_token_num=5, chunk_size_sec=0.5)
    s = BoundedOfficialStream(
        asr, state, sampling_params_factory=lambda n: n,
        parse_output=lambda raw, lang: parse_asr_output(raw, user_language=lang), pcm=numpy_pcm_ops(),
    )
    for _ in pieces:
        s.feed(np.zeros(SR // 2, dtype=np.float32))
    s.finish()
    assert s.state.text == "".join(pieces)


def test_engine_transcribe_uses_same_bounded_kernel_once_and_returns_full_text():
    pieces = ["大家好，", "今天开会。", "好的，", "好的。", "Thanks ", "everyone."]
    eng, fake = _engine(_Budget(_speech(pieces)))
    out = eng.transcribe(_pcm(0.5 * len(pieces)), language="zh", context="ctx")
    assert out.text == "".join(pieces) and out.language == "Chinese"
    assert fake.init_calls == [{"context": "ctx", "language": "Chinese", "chunk_size_sec": DEFAULT_CHUNK_SIZE_SEC}]
    assert all(r.max_tokens < 4096 for r in eng.last_transcribe_reports)


def test_engine_transcribe_runaway_is_explicit_failure_not_truncated_success():
    pieces = ["甲乙", "丙丁", "戊己", "庚辛", "壬癸", "子丑"]
    eng, _ = _engine(_Budget(_speech(pieces), runaway_at={n * SR // 2 for n in range(4, 7)}))
    with pytest.raises(LogicError) as err:
        eng.transcribe(_pcm(3.0))
    assert err.value.code == ErrorCode.asr_unavailable


def test_engine_transcribe_rejects_over_session_limit_and_pads_short():
    eng, _ = _engine(_Budget(_speech(["嗯。"])))
    with pytest.raises(LogicError) as err:
        eng.transcribe(_pcm(120.5))
    assert err.value.code == ErrorCode.invalid_request
    assert eng.transcribe(_pcm(0.1)).text == "嗯。"
    assert eng.model_path  # 未触碰真实模型


def test_stream_and_transcribe_share_kernel_construction():
    pieces = ["一二", "三四", "五六"]
    eng, fake = _engine(_Budget(_speech(pieces)))
    eng.start_stream("s1", language="zh")
    for _ in pieces:
        eng.push_pcm("s1", _pcm(0.5))
    streamed = eng.finish_stream("s1").text
    assert streamed == eng.transcribe(_pcm(1.5), language="zh").text == "一二三四五六"
    assert len(fake.init_calls) == 2


def test_default_engine_stream_and_transcribe_use_0_4_5_5_and_state_budget():
    # 真实默认构造路径（OfficialEngine() 无参数），不是测试注入覆盖。
    pieces = [f"第{i}段，" for i in range(12)]
    step = int(round(0.4 * SR))
    llm = _Budget(lambda n: "".join(pieces[: -(-n // step)]))
    eng, fake = _engine(llm)
    assert (eng.chunk_size_sec, eng.unfixed_chunk_num, eng.unfixed_token_num) == (0.4, 5, 5)
    eng.start_stream("s1", language="zh")
    for _ in pieces:
        eng.push_pcm("s1", _pcm(0.4))
    dec = eng._streams["s1"].decoder
    assert dec.state.chunk_size_samples == step and len(dec.reports) == len(pieces)
    assert dec.reports[-1].rollback_tokens == 5 and dec.reports[-1].max_tokens == 50
    assert eng.finish_stream("s1").text == "".join(pieces)
    out = eng.transcribe(_pcm(0.4 * len(pieces) + 0.3), language="zh")  # 末块不对齐
    assert out.text == "".join(pieces)
    assert fake.init_params == [(0.4, 5, 5), (0.4, 5, 5)]
    assert all(r.max_tokens < 4096 for r in eng.last_transcribe_reports)
