# ASR sidecar

本目录是官方 Qwen3-ASR 的独立进程宿主，监听 **127.0.0.1:17946**。
HTTP 协议与应用侧 `_asr.AsrSidecarClient`、`_contracts` 中的 `SIDECAR_*` 一致，不是第二套 API。

## 路径

- `POST /v1/transcribe` 整段转写（multipart `audio`，可选 `language` / `context`）→ `{ok, text, language}`；
  超过 120s 以 invalid_request 拒绝，解码失控以 asr_unavailable（503）失败，不返回半截文本
- `POST /v1/sessions` → `{session_id}`
- `POST /v1/sessions/{id}/chunks`  `Content-Type: application/octet-stream`，`X-Chunk-Index`
- `GET /v1/sessions/{id}/events`  SSE：`partial` / `final` / `error` / `closed`
- `POST /v1/sessions/{id}/finish` → `final`
- `POST /v1/sessions/{id}/cancel` → `closed(cancelled)`
- `GET /health` 进程探活，含 `unmet_blockers`，不得当成可上线

## 启动

必须用隔离 Python 3.12.13（`.cache/v3-feasibility/venv-asr`），且加 `--load` 才会占 GPU。
`import runtimes.asr` 本身不加载 torch。

长音频：每个 chunk 仍按官方算法全量重喂 `audio_accum`，实测 118s 时单步 ≤0.23s，不是瓶颈；
81–85s 的崖壁是模型在复读前缀上失控、一直生成到官方整段上限 4096 token。现由
`stream_step.BoundedOfficialStream` 给每步加语速上界并拒绝失控步（见下）。4b 并存未测。
不要用跨窗字符串拼接冒充验收。

## Partial 策略

官方 `ASRStreamingState` 只更新 `text` 和 `language`。`unfixed_token_num` 只用于内部 prompt 回退。
本宿主把当前官方文本放在 `tail_text`，`stable_text` 为空，直到 `BoundedOfficialStream.finish`（官方 `finish_streaming_transcribe` 的有界实现）定稿。

## 每步解码（stream_step.py）

算法与官方 `streaming_transcribe` / `finish_streaming_transcribe` 相同（同一 state、prompt、前缀回退），只多：

- 步长默认 `chunk_size_sec=0.4`、`unfixed_chunk_num=5`（无前缀窗口 2.0s，与旧 0.5×4 相同），唯一来源 `protocol.py`；
  桌面上传节拍 `_capture.CHUNK_S` 引用同一常量。依据见 r8：0.5 时连说中会连续 3 步同文（最长 2.01s）。
- 每步 `max_tokens` = 回退 token + 16 + ceil(12 token/s ×（chunk_size_sec + 2s 滞后余量）)，按 state 实际步长计算，0.4s 时为 50；无前缀的前几步按全部音频计。
- vLLM `finish_reason == "length"` 的步不写入 `_raw_decoded`，字幕停在最后可信结果；音频不丢，下一步整体重识别。
- 定稿时若有未覆盖的音频（被拒步），即使没有尾音频也再解码，上界按文字最后一次增长以来的音频放宽；
  仍被截断则 `finish` 返回 503 并关闭会话，不把旧字幕当定稿。
- 文本解析用 `asr_text.parse_asr_text`：与官方 `parse_asr_output` 的语言/文本边界相同，但**不做**官方
  `detect_and_fix_repetitions(threshold=20)` 折叠（它会删掉用户真实连说 ≥20 次的短句）。失控由上面的
  `length` 拒绝处理，不靠删文本。上限以内模型自己产生的重复会原样呈现，那仍是识别错误，不是被“修好”。

## 整段转写（手机 from-audio）

官方一次性 `transcribe` 同样会在长音频上失控到 4096 token（实测 118s 中英混说只剩 106 字）。
`OfficialEngine.transcribe` 因此不再调用它，而是用与流式相同的 `_new_decoder` + `stream_step.decode_whole`：
把整段按同一步长（默认 0.4s）不配速喂给同一个有界内核再定稿。不足 0.5s 的音频补零到 0.5s（官方 MIN_ASR_INPUT_SECONDS）。外部协议仍是一次上传一次返回。
代价：0.5s 步长时 118s 音频约 22–29s（一次性官方约 2s，但会丢文本）；0.4s 步长的实测耗时见 r9 证据。短段文本与一次性官方不保证逐字相同。

## 测试与依赖

- `tests/sidecar/test_stream_step.py`、`tests/sidecar/test_asr_text.py`：纯算法（假模型、list PCM），不需要 numpy/torch，在常规闸门
  `scripts/gate.py --layer sidecar` 中运行。
- `tests/sidecar/test_stream_step_official.py`：需要官方 `qwen_asr`/numpy，不加载 GPU，只在隔离环境里单独运行：
  `.cache/v3-feasibility/venv-asr/bin/python -m pytest -q tests/sidecar/test_stream_step_official.py`
  （pytest 仅装在该 venv 内：`uv pip install --python .cache/v3-feasibility/venv-asr/bin/python pytest`）。
  不要与其他 sidecar 测试同一进程运行，那些测试会断言不导入 torch。

