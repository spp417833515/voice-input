"""Sidecar 与 `_asr.AsrSidecarClient` 共用的路径、默认参数和 partial 策略说明。

官方 `ASRStreamingState` 只公开 `text` / `language`（另有内部 `_raw_decoded`）。
`unfixed_token_num` 只用于官方 Prompt 前缀回退，不是对外稳定前沿字段。
因此流式 partial 把当前官方 `text` 全部放在 `tail_text`，`stable_text` 保持空，
直到有界内核 `stream_step.BoundedOfficialStream.finish`（官方 finish_streaming_transcribe 的受控实现）给出定稿；不得自行按最后 K 个 token 切 stable。
"""
from __future__ import annotations

from _contracts import (
    ASR_STREAMING_BLOCKERS,
    SIDECAR_CHUNK_HEADER,
    SIDECAR_DEFAULT_BASE_URL,
    SIDECAR_SESSIONS_PATH,
    SIDECAR_TRANSCRIBE_PATH,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17946
DEFAULT_GPU_MEMORY_UTILIZATION = 0.8
DEFAULT_MAX_MODEL_LEN = 16384
# 官方 init_streaming_state 受支持参数。0.4s × 5 块 = 2.0s 无前缀窗口，与旧 0.5s × 4 相同。
# 选 0.4 的依据（r8，见 v3-plan-evidence/v3-asr-r8-repeat-gap.md）：0.5 时模型在真实连说上会连续 3 步
# 同文（Testing×22 间隔 2.01s、Libri 23s 词界 1.50s），0.4 时同样位置为 1.20/1.21s，定稿不变。
# 这是唯一参数源：sidecar 解码步长、手机整段内核步长与桌面上传节拍（_capture.CHUNK_S）都引用它。
DEFAULT_CHUNK_SIZE_SEC = 0.4
DEFAULT_UNFIXED_CHUNK_NUM = 5
DEFAULT_UNFIXED_TOKEN_NUM = 5
DEFAULT_SAMPLE_RATE = 16000
# 与产品 120s 看门狗对齐的音频缓存上限，超限拒绝，不在 sidecar 做跨窗拼接。
DEFAULT_MAX_SESSION_SAMPLES = DEFAULT_SAMPLE_RATE * 120
DEFAULT_IDLE_S = 180.0

OFFICIAL_PARTIAL_POLICY = (
    "官方 ASRStreamingState 无独立 stable 输出，只更新 state.text / state.language。"
    "unfixed_token_num 仅用于内部 prefix 回退，不是可引用的稳定前沿。"
    "本 sidecar 的 SessionPartialEvent.stable_text 保持空字符串，tail_text 为当前官方 text；"
    "定稿只来自 BoundedOfficialStream.finish（官方 finish_streaming_transcribe 的有界实现）。不得用字符串切片冒充稳定。"
)

LANGUAGE_ALIASES = {
    "zh": "Chinese",
    "zh-cn": "Chinese",
    "chi": "Chinese",
    "chinese": "Chinese",
    "en": "English",
    "eng": "English",
    "english": "English",
    "ja": "Japanese",
    "jp": "Japanese",
    "japanese": "Japanese",
    "ko": "Korean",
    "korean": "Korean",
    "yue": "Cantonese",
    "cantonese": "Cantonese",
}


def normalize_language(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    key = str(value).strip()
    return LANGUAGE_ALIASES.get(key.lower(), key)


__all__ = [
    "ASR_STREAMING_BLOCKERS",
    "DEFAULT_CHUNK_SIZE_SEC",
    "DEFAULT_GPU_MEMORY_UTILIZATION",
    "DEFAULT_HOST",
    "DEFAULT_IDLE_S",
    "DEFAULT_MAX_MODEL_LEN",
    "DEFAULT_MAX_SESSION_SAMPLES",
    "DEFAULT_PORT",
    "DEFAULT_SAMPLE_RATE",
    "DEFAULT_UNFIXED_CHUNK_NUM",
    "DEFAULT_UNFIXED_TOKEN_NUM",
    "OFFICIAL_PARTIAL_POLICY",
    "SIDECAR_CHUNK_HEADER",
    "SIDECAR_DEFAULT_BASE_URL",
    "SIDECAR_SESSIONS_PATH",
    "SIDECAR_TRANSCRIBE_PATH",
    "normalize_language",
]
