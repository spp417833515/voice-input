"""Qwen3-ASR 原始输出的「语言元信息 / 转写文本」边界解析，不折叠重复。

与 qwen_asr 0.0.6 `inference/utils.py::parse_asr_output` 的标签与语言处理逐行一致，唯一区别：
不调用 `detect_and_fix_repetitions(threshold=20)`。官方那一步会把连续 ≥20 次、≤20 字的片段
（或连续 >20 个同一字符）折叠成一次，用户真的连说 20 遍同一句时也会被解析器删掉。

本宿主由 `stream_step.BoundedOfficialStream` 按语速上界拒绝 `finish_reason == "length"` 的失控步，
失控续写本来就不会写入历史，因此这里不再需要靠删文本来压复读。这只修「解析器人工删文本」：
模型若在上限以内产生幻觉式重复，文本会原样呈现，这仍是识别错误，不代表模型输出正确。

语言名规范化复用官方 `normalize_language_name`，由调用方注入（生产见 engine._new_decoder），
本模块因此不依赖 qwen_asr，可在闸门环境做纯算法测试。标签常量与官方一致，由官方测试核对。
"""
from __future__ import annotations

from collections.abc import Callable

ASR_TEXT_TAG = "<asr_text>"
LANG_PREFIX = "language "


def parse_asr_text(
    raw: str | None,
    user_language: str | None,
    *,
    normalize_language_name: Callable[[str], str],
) -> tuple[str, str]:
    """返回 (language, text)。分支与官方 parse_asr_output 相同，只是不折叠重复。"""
    if raw is None:
        return "", ""
    s = str(raw).strip()
    if not s:
        return "", ""
    if user_language:
        # 强制语言时模型只输出纯文本
        return user_language, s
    if ASR_TEXT_TAG not in s:
        return "", s.strip()
    meta_part, text_part = s.split(ASR_TEXT_TAG, 1)
    if "language none" in meta_part.lower():
        # 空音频启发式：仍有文字则保留文字、语言未知
        t = text_part.strip()
        return ("", "") if not t else ("", t)
    lang = ""
    for line in meta_part.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith(LANG_PREFIX):
            val = line[len(LANG_PREFIX):].strip()
            if val:
                lang = normalize_language_name(val)
            break
    return lang, text_part.strip()
