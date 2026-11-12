"""ASR sidecar 宿主。本包在 import 时不得加载 torch / vLLM / 模型。

官方引擎只在显式 `serve --load` 或 OfficialEngine.load() 时进入本进程。
协议与应用侧 `_asr.AsrSidecarClient` / `_contracts` 路径一致，不另起一套旁支。
长音频 0.5–1s / 120s 与 4b 并存仍阻塞，见 ASR_STREAMING_BLOCKERS。
"""

from runtimes.asr.protocol import OFFICIAL_PARTIAL_POLICY

__all__ = ["OFFICIAL_PARTIAL_POLICY"]
