"""v1 契约的唯一来源：请求/响应、枚举、结构化错误、两类 SSE 事件 schema。

本模块只描述数据形状，不含业务规则、鉴权或路由。
OpenAPI 的 components.schemas 必须由这些模型生成，禁止手写第二份。
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel


class ErrorCode(StrEnum):
    invalid_request = "invalid_request"
    not_found = "not_found"
    conflict = "conflict"
    unauthorized = "unauthorized"
    forbidden = "forbidden"
    timeout = "timeout"
    dependency_failed = "dependency_failed"
    asr_unavailable = "asr_unavailable"
    polish_failed = "polish_failed"
    translate_failed = "translate_failed"
    ocr_failed = "ocr_failed"
    ocr_language_missing = "ocr_language_missing"
    history_io = "history_io"
    jobs_io = "jobs_io"
    delivery_failed = "delivery_failed"
    config_io = "config_io"
    internal = "internal"


class ErrorBody(BaseModel):
    """所有能力接口失败时的结构化错误体。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[False] = False
    error: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class LogicError(Exception):
    """逻辑层可见失败。调用方应把它转成 ErrorBody，不得吞掉后返回原文充成功。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_body(self) -> ErrorBody:
        return ErrorBody(error=self.code, message=self.message, details=self.details)


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class JobState(StrEnum):
    """jobs.json 持久化状态。转移规则属于业务层，本层只约束取值。

    failed 仅表示已确定本次毫无输出。
    delivery_unknown 覆盖部分输出、超时、ack 丢失等未确定投递结果。
    查询不存在的 job 时用 JobQueryUnknown.state="unknown"，不得把 unknown 写入 JobRecord。
    """

    queued = "queued"
    claimed = "claimed"
    done = "done"
    failed = "failed"
    delivery_unknown = "delivery_unknown"


class AckResult(StrEnum):
    """投递 ack 的结果取值（业务层解释；存储层原样保存）。"""

    complete = "complete"
    none = "none"
    partial_or_unknown = "partial_or_unknown"


class UtteranceSource(StrEnum):
    desktop = "desktop"
    web = "web"
    resend = "resend"


class SessionClosedReason(StrEnum):
    finished = "finished"
    cancelled = "cancelled"
    error = "error"
    timeout = "timeout"
    disconnected = "disconnected"


class SessionEventType(StrEnum):
    partial = "partial"
    final = "final"
    error = "error"
    closed = "closed"


class ProductEventType(StrEnum):
    """产品事件流（状态栏等）的类型判别，与识别会话 SSE 分开。不用自由文本 style 冒充事件。"""

    idle = "idle"
    recording = "recording"
    partial = "partial"
    polishing = "polishing"
    typed = "typed"
    error = "error"
    busy = "busy"
    history_write_failed = "history_write_failed"
    delivery_unknown = "delivery_unknown"


class HistoryPolish(StrEnum):
    """历史记录中的润色结果。与旧字段 mode 分离，不互相当别名。"""

    on = "on"
    off = "off"
    failed = "failed"
    skipped = "skipped"


# ---------------------------------------------------------------------------
# 识别会话（产品 /api/v1/recognize/* 与 sidecar 共用字段形状）
# ---------------------------------------------------------------------------


class RecognizeSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str | None = None
    context: str | None = None


class RecognizeSessionCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str


class ChunkAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: Literal[True] = True
    index: int = Field(ge=0)


class SessionPartialEvent(BaseModel):
    """会话 partial 的唯一字段是 stable_text / tail_text，不并行维护 stable/tail 或独立 text。"""

    model_config = ConfigDict(extra="forbid")
    event: Literal["partial"] = "partial"
    session_id: str
    stable_text: str = ""
    tail_text: str = ""
    language: str | None = None
    audio_end_s: float | None = None


class SessionFinalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["final"] = "final"
    session_id: str
    text: str
    language: str | None = None


class SessionErrorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["error"] = "error"
    session_id: str
    error: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class SessionClosedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["closed"] = "closed"
    session_id: str
    reason: SessionClosedReason


SessionSSEEvent = Annotated[
    Union[SessionPartialEvent, SessionFinalEvent, SessionErrorEvent, SessionClosedEvent],
    Field(discriminator="event"),
]


class SessionSSEEventEnvelope(RootModel[SessionSSEEvent]):
    """识别会话 SSE 一条事件的显式信封，供 OpenAPI components.schemas 引用。"""


# ---------------------------------------------------------------------------
# 产品事件 SSE（只定义表示形状，不含业务状态转移）
# ---------------------------------------------------------------------------


class ProductIdleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.idle] = ProductEventType.idle


class ProductRecordingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.recording] = ProductEventType.recording
    session_id: str | None = None


class ProductPartialEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.partial] = ProductEventType.partial
    session_id: str | None = None
    job_id: str | None = None
    stable_text: str = ""
    tail_text: str = ""


class ProductPolishingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.polishing] = ProductEventType.polishing
    job_id: str | None = None


class ProductTypedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.typed] = ProductEventType.typed
    job_id: str
    text: str = ""


class ProductErrorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.error] = ProductEventType.error
    error: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class ProductBusyEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.busy] = ProductEventType.busy


class ProductHistoryWriteFailedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.history_write_failed] = ProductEventType.history_write_failed
    job_id: str | None = None
    message: str
    error: ErrorCode | None = None


class ProductDeliveryUnknownEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.delivery_unknown] = ProductEventType.delivery_unknown
    job_id: str
    text: str | None = None


ProductSSEEvent = Annotated[
    Union[
        ProductIdleEvent,
        ProductRecordingEvent,
        ProductPartialEvent,
        ProductPolishingEvent,
        ProductTypedEvent,
        ProductErrorEvent,
        ProductBusyEvent,
        ProductHistoryWriteFailedEvent,
        ProductDeliveryUnknownEvent,
    ],
    Field(discriminator="event"),
]


class ProductSSEEventEnvelope(RootModel[ProductSSEEvent]):
    """产品事件 SSE 一条事件的显式信封。"""


PlatformEvent = Annotated[
    Union[ProductIdleEvent, ProductBusyEvent, ProductErrorEvent],
    Field(discriminator="event"),
]


class PlatformEventCreate(RootModel[PlatformEvent]):
    """本机桌面平台壳上报的状态（开麦失败、截图中、截图失败等），原样进入产品事件流。

    只允许 idle / busy / error；识别、润色、投递状态由服务端业务自己发布，平台不得冒充。
    """


class PlatformEventAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True


# ---------------------------------------------------------------------------
# Utterance / Job（契约形状；业务编排不在本层）
# ---------------------------------------------------------------------------


class UtteranceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    session_id: str
    source: UtteranceSource
    focus_id: str = Field(description="按 job 绑定的焦点，禁止使用全局唯一焦点变量")


class UtteranceFromAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    source: Literal[UtteranceSource.web] = UtteranceSource.web
    language: str | None = None
    context: str | None = None


class UtteranceAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    state: Literal[JobState.queued] = JobState.queued
    text: str
    job_id: str


class UtteranceResendRequest(BaseModel):
    """original_job_id 必填：不得靠省略原 id 绕过 delivery_unknown 确认。"""

    model_config = ConfigDict(extra="forbid")
    job_id: str
    text: str
    original_job_id: str = Field(min_length=1)
    confirm: bool = False
    focus_id: str | None = Field(
        default=None,
        description="覆盖继承自原 job 的焦点。桌面重复键传按键时的焦点；省略则沿用原 job 焦点",
    )


class UtteranceResendQueued(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    state: Literal[JobState.queued] = JobState.queued
    job_id: str
    text: str


class UtteranceResendNeedsConfirmation(BaseModel):
    """未确认的 resend 响应。ok 必须为 false；是否入队由业务层执行，本模型不得表示已成功入队。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[False] = False
    requires_confirmation: Literal[True] = True
    original_job_id: str
    new_job_id: str


class JobClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_s: float | None = Field(default=None, ge=0)


class JobClaimed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    claim_id: str
    text: str
    source: UtteranceSource
    focus_id: str
    state: Literal[JobState.claimed] = JobState.claimed


class JobAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    result: AckResult
    detail: str | None = None


class JobAckAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    job_id: str
    state: JobState


class JobRecord(BaseModel):
    """jobs.json 中一条记录的 schema。focus_id 跟 job，不是全局单值。state 不含 unknown。"""

    model_config = ConfigDict(extra="allow")
    job_id: str
    state: JobState
    text: str = ""
    source: UtteranceSource = UtteranceSource.desktop
    focus_id: str = ""
    session_id: str | None = None
    claim_id: str | None = None
    created_at: str
    updated_at: str
    detail: str | None = None
    original_job_id: str | None = None


class JobStoreDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    jobs: dict[str, JobRecord] = Field(default_factory=dict)


class JobQueryFound(BaseModel):
    """GET job 在记录存在时的查询响应。state 来自持久化 JobRecord，不可能是 unknown。"""

    model_config = ConfigDict(extra="forbid")
    found: Literal[True] = True
    job: JobRecord


class JobQueryUnknown(BaseModel):
    """GET job 在记录不存在时的查询响应。unknown 只出现在此查询形状，不可写入 JobRecord。"""

    model_config = ConfigDict(extra="forbid")
    found: Literal[False] = False
    job_id: str
    state: Literal["unknown"] = "unknown"


JobQueryResponse = Annotated[
    Union[JobQueryFound, JobQueryUnknown],
    Field(discriminator="found"),
]


class JobQueryResponseEnvelope(RootModel[JobQueryResponse]):
    """job 查询响应信封，供 OpenAPI 引用。"""


# ---------------------------------------------------------------------------
# 能力接口
# ---------------------------------------------------------------------------


class SttRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str | None = None
    context: str | None = None


class SttResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    text: str
    language: str | None = None


class PolishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    keywords: list[str] | None = None
    language: str | None = None


class PolishResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    text: str


class TranslateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    target_lang: str


class TranslateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    text: str
    target_lang: str


class OcrTranslateResponse(BaseModel):
    """OCR+翻译成功体：text 是译文，ocr_text 是识别原文；不得把 ocr 当 text。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    text: str
    ocr_text: str
    target_lang: str


# ---------------------------------------------------------------------------
# 历史（处理记录，不是投递记录）
# ---------------------------------------------------------------------------


class HistoryRecord(BaseModel):
    """新记录显式使用 source / polish / job_id。旧 mode 仅兼容读取，不是这些字段的别名。"""

    model_config = ConfigDict(extra="allow")
    timestamp: str
    raw_text: str
    final_text: str
    language: str
    mode: str | None = None
    source: UtteranceSource | None = None
    polish: HistoryPolish | None = None
    job_id: str | None = None


class HistoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[HistoryRecord]
    total: int
    skipped_malformed: int = 0


# ---------------------------------------------------------------------------
# 配置（已知字段；未知字段在存取层保留）
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """持久化配置的已知字段。未在此声明的键由存取层原样保留。

    默认值只用于缺省键，不会在加载时改写磁盘。不把未选定项写成用户已选事实。
    """

    model_config = ConfigDict(extra="allow")
    ollama_model: str = "qwen3.5:4b"
    whisper_model: str = "small"
    whisper_language: str = "zh"
    custom_keywords: list[str] = Field(default_factory=list)
    translate_target_lang: str = "zh"
    baidu_appid: str = ""
    baidu_appkey: str = ""
    hotkey_voice: str = "ctrl+alt+z"
    hotkey_screenshot: str = "ctrl+alt+x"
    hotkey_repeat: str = "ctrl+alt+r"
    vision_model: str | None = None
    voice_mode: str | None = None
    asr_sidecar_url: str | None = None
    ollama_base_url: str | None = None
    polish_enabled: bool | None = None



class ApiStatusResponse(BaseModel):
    """当前运行时状态。unmet_gates 非空即表示未达切流条件，不得把 ok 当成可上线。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = Field(
        True,
        description="本接口响应成功，不是可上线或已切流。切流条件只看 unmet_gates。",
    )
    service: Literal["ollama-voice-input"] = "ollama-voice-input"
    unmet_gates: list[str] = Field(default_factory=list, description="未满足的切流条件；非空即不可上线。")
    injected: dict[str, bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 产品固定模型与配置视图（API 只暴露这些字段，不回传旧密钥）
# ---------------------------------------------------------------------------

PRODUCT_OLLAMA_MODEL = "qwen3.5:4b"
PRODUCT_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"


class AppConfigView(BaseModel):
    """GET/POST /api/v1/config 的响应。只含产品可见字段；百度密钥、Whisper 等旧键留在磁盘但不回传。"""

    model_config = ConfigDict(extra="forbid")
    polish_enabled: bool
    custom_keywords: list[str]
    translate_target_lang: str
    hotkey_voice: str
    hotkey_screenshot: str
    hotkey_repeat: str
    ollama_model: str = Field(description="产品固定纠错/翻译模型，只读")


class AppConfigUpdate(BaseModel):
    """POST /api/v1/config 请求体：唯一可写字段集合，未知字段拒绝。"""

    model_config = ConfigDict(extra="forbid")
    polish_enabled: bool | None = None
    custom_keywords: list[str] | None = None
    translate_target_lang: str | None = Field(default=None, min_length=1)
    hotkey_voice: str | None = Field(default=None, min_length=1)
    hotkey_screenshot: str | None = Field(default=None, min_length=1)
    hotkey_repeat: str | None = Field(default=None, min_length=1)


# ---------------------------------------------------------------------------
# 设置：真实状态检查与异步安装/下载任务
# ---------------------------------------------------------------------------


class SetupComponentState(StrEnum):
    missing = "missing"
    downloading = "downloading"
    ready = "ready"
    failed = "failed"


class SetupTaskKind(StrEnum):
    asr_download = "asr_download"
    asr_start = "asr_start"
    ollama_install = "ollama_install"
    ollama_start = "ollama_start"
    ollama_pull = "ollama_pull"


class SetupTaskState(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class SetupProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    percent: float | None = Field(default=None, ge=0, le=100)
    stage: str | None = None


class SetupTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: SetupTaskKind


class SetupTask(BaseModel):
    """一次真实安装/下载/启动作业。succeeded 只在动作真实完成并复查通过后出现。"""

    model_config = ConfigDict(extra="forbid")
    task_id: str
    kind: SetupTaskKind
    state: SetupTaskState
    progress: SetupProgress | None = None
    message: str = ""
    error: ErrorCode | None = None
    started_at: str
    finished_at: str | None = None


class AsrSetupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_name: str = PRODUCT_ASR_MODEL
    status: SetupComponentState
    weights_present: bool
    sidecar_running: bool
    size_label: str | None = None
    device_label: str | None = None
    progress: SetupProgress | None = None
    message: str = ""
    action: SetupTaskKind | None = None
    task_id: str | None = None


class OllamaSetupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_name: str = PRODUCT_OLLAMA_MODEL
    installed: bool
    version: str | None = None
    running: bool
    model_status: SetupComponentState
    size_label: str | None = None
    progress: SetupProgress | None = None
    message: str = ""
    action: SetupTaskKind | None = None
    task_id: str | None = None


class SetupCapability(BaseModel):
    """系统能力项（剪贴板、Wayland portal 截图/快捷键、X11 maim、OCR 语言等）。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    available: bool
    required: bool = True
    detail: str | None = None


class SetupCheckResponse(BaseModel):
    """ok 只表示检查本身成功；组件是否就绪看 asr/ollama/capabilities，是否可上线看 unmet_gates。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    session_type: str
    asr: AsrSetupStatus
    ollama: OllamaSetupStatus
    capabilities: list[SetupCapability]
    unmet_gates: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 手机局域网入口（仅本机签发）
# ---------------------------------------------------------------------------


class LanAccessEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ip: str
    url: str = Field(description="含 token 的手机录音页地址；只在本机响应中出现")
    url_masked: str
    qr_svg: str = Field(description="url 的二维码 SVG 文本")


class LanAccessResponse(BaseModel):
    """GET /api/v1/lan-access：仅本机。token 只供用户点击复制，不得写入日志或示例。"""

    model_config = ConfigDict(extra="forbid")
    port: int
    token: str
    token_masked: str
    entries: list[LanAccessEntry]
    message: str = ""


# ---------------------------------------------------------------------------
# Sidecar HTTP 路径（应用核心只通过这些路径访问 ASR，不 import torch/vLLM）
# ---------------------------------------------------------------------------

SIDECAR_DEFAULT_BASE_URL = "http://127.0.0.1:17946"
SIDECAR_TRANSCRIBE_PATH = "/v1/transcribe"
SIDECAR_SESSIONS_PATH = "/v1/sessions"
SIDECAR_CHUNK_HEADER = "X-Chunk-Index"

# 流式长录音刷新（原 P2）已按用户口径验收（2026-11-11 03:03 America/New_York，证据
# v3-asr-r9-candidate-acceptance.md / v3-asr-r9-default-recheck.md：默认 0.4/5/5，真实 TLS uvicorn→provider→1.7B GPU，
# 六条近 120s 连续说话最大间隔 0.749–1.226s）。旧 p95=1.515s/1.966s 阻塞文案已由该证据关闭。
# 以下为仍然成立的限制（health.unmet_blockers 原样发布），不得据此宣称 ASR 全部能力或产品已可用。
ASR_STREAMING_BLOCKERS = (
    "有声硬帽字符串拼接会漏字/重复；不得把跨窗 suffix/prefix 合并当成正确。",
    "刷新仅按用户口径验收（通常 0.5–1s，词句交界偶发约 1.5s；r9 默认 0.4/5/5 六条近 120s 连续说话最大 0.749–1.226s），"
    "不等于严格 p95≤1s；超过 120s 单会话不支持；与 qwen3.5:4b 同机并存、真实设备/WebKit 未验收。",
    "长串同句重复计数可能多或少一次（模型识别错误；r10：九点×21 在 0.4/5/5 稳定多一句，属切分参数相关），"
    "不承诺零错，也不据此推出一般准确率退化。",
)


def contract_models() -> tuple[type[BaseModel], ...]:
    """所有需要进入 OpenAPI components.schemas 的模型（含两类 SSE）。"""
    return (
        ErrorBody,
        RecognizeSessionCreate,
        RecognizeSessionCreated,
        ChunkAccepted,
        SessionPartialEvent,
        SessionFinalEvent,
        SessionErrorEvent,
        SessionClosedEvent,
        SessionSSEEventEnvelope,
        ProductIdleEvent,
        ProductRecordingEvent,
        ProductPartialEvent,
        ProductPolishingEvent,
        ProductTypedEvent,
        ProductErrorEvent,
        ProductBusyEvent,
        ProductHistoryWriteFailedEvent,
        ProductDeliveryUnknownEvent,
        ProductSSEEventEnvelope,
        PlatformEventCreate,
        PlatformEventAccepted,
        UtteranceCreate,
        UtteranceFromAudio,
        UtteranceAccepted,
        UtteranceResendRequest,
        UtteranceResendQueued,
        UtteranceResendNeedsConfirmation,
        JobClaimRequest,
        JobClaimed,
        JobAckRequest,
        JobAckAccepted,
        JobRecord,
        JobStoreDocument,
        JobQueryFound,
        JobQueryUnknown,
        JobQueryResponseEnvelope,
        SttRequest,
        SttResponse,
        PolishRequest,
        PolishResponse,
        TranslateRequest,
        TranslateResponse,
        OcrTranslateResponse,
        HistoryRecord,
        HistoryListResponse,
        AppConfig,
        AppConfigView,
        AppConfigUpdate,
        ApiStatusResponse,
        SetupProgress,
        SetupTaskCreate,
        SetupTask,
        AsrSetupStatus,
        OllamaSetupStatus,
        SetupCapability,
        SetupCheckResponse,
        LanAccessEntry,
        LanAccessResponse,
    )


def build_openapi_skeleton() -> dict[str, Any]:
    """由模型生成 OpenAPI 3 骨架：只有 components.schemas，没有虚构已实现的 paths。

    集成阶段必须再与 FastAPI 导出的 /openapi.json 交叉核对；本函数不宣称 API 全通过。
    """
    from pydantic.json_schema import models_json_schema

    _, tree = models_json_schema(
        [(model, "validation") for model in contract_models()],
        ref_template="#/components/schemas/{model}",
    )
    defs = tree.get("$defs") or tree.get("definitions") or {}
    schemas = {name: defs[name] for name in sorted(defs)}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "ollama-voice-input v1 contracts",
            "version": "0.0.0-l1",
            "description": (
                "Generated from _contracts.py. Paths are intentionally empty until "
                "routes exist. SSE payloads are in components.schemas."
            ),
        },
        "paths": {},
        "components": {"schemas": schemas},
    }


__all__ = [
    "AppConfigView",
    "AppConfigUpdate",
    "AsrSetupStatus",
    "LanAccessEntry",
    "LanAccessResponse",
    "OllamaSetupStatus",
    "PRODUCT_ASR_MODEL",
    "PRODUCT_OLLAMA_MODEL",
    "SetupCapability",
    "SetupCheckResponse",
    "SetupComponentState",
    "SetupProgress",
    "SetupTask",
    "SetupTaskCreate",
    "SetupTaskKind",
    "SetupTaskState",
    "ASR_STREAMING_BLOCKERS",
    "AckResult",
    "ApiStatusResponse",
    "AppConfig",
    "ChunkAccepted",
    "ErrorBody",
    "ErrorCode",
    "HistoryListResponse",
    "HistoryPolish",
    "HistoryRecord",
    "JobAckAccepted",
    "JobAckRequest",
    "JobClaimRequest",
    "JobClaimed",
    "JobQueryFound",
    "JobQueryResponse",
    "JobQueryResponseEnvelope",
    "JobQueryUnknown",
    "JobRecord",
    "JobState",
    "JobStoreDocument",
    "LogicError",
    "OcrTranslateResponse",
    "PolishRequest",
    "PolishResponse",
    "ProductBusyEvent",
    "ProductDeliveryUnknownEvent",
    "ProductErrorEvent",
    "ProductEventType",
    "ProductHistoryWriteFailedEvent",
    "ProductIdleEvent",
    "ProductPartialEvent",
    "ProductPolishingEvent",
    "ProductRecordingEvent",
    "ProductSSEEvent",
    "ProductSSEEventEnvelope",
    "ProductTypedEvent",
    "RecognizeSessionCreate",
    "RecognizeSessionCreated",
    "SIDECAR_CHUNK_HEADER",
    "SIDECAR_DEFAULT_BASE_URL",
    "SIDECAR_SESSIONS_PATH",
    "SIDECAR_TRANSCRIBE_PATH",
    "SessionClosedEvent",
    "SessionClosedReason",
    "SessionErrorEvent",
    "SessionEventType",
    "SessionFinalEvent",
    "SessionPartialEvent",
    "SessionSSEEvent",
    "SessionSSEEventEnvelope",
    "SttRequest",
    "SttResponse",
    "TranslateRequest",
    "TranslateResponse",
    "UtteranceAccepted",
    "UtteranceCreate",
    "UtteranceFromAudio",
    "UtteranceResendNeedsConfirmation",
    "UtteranceResendQueued",
    "UtteranceResendRequest",
    "UtteranceSource",
    "build_openapi_skeleton",
    "contract_models",
]
