from __future__ import annotations

import pytest
from pydantic import ValidationError

from _contracts import (
    ASR_STREAMING_BLOCKERS,
    AckResult,
    AppConfig,
    ErrorBody,
    ErrorCode,
    HistoryPolish,
    HistoryRecord,
    JobAckRequest,
    JobQueryFound,
    JobQueryResponseEnvelope,
    JobQueryUnknown,
    JobRecord,
    JobState,
    PolishResponse,
    ProductBusyEvent,
    ProductDeliveryUnknownEvent,
    ProductErrorEvent,
    ProductEventType,
    ProductHistoryWriteFailedEvent,
    ProductIdleEvent,
    ProductPartialEvent,
    ProductPolishingEvent,
    ProductRecordingEvent,
    ProductSSEEventEnvelope,
    ProductTypedEvent,
    SessionClosedEvent,
    SessionClosedReason,
    SessionPartialEvent,
    SessionSSEEventEnvelope,
    SttResponse,
    TranslateResponse,
    UtteranceResendNeedsConfirmation,
    UtteranceResendRequest,
    UtteranceSource,
    build_openapi_skeleton,
    contract_models,
)


def test_error_body_and_logic_shape():
    body = ErrorBody(error=ErrorCode.timeout, message="x")
    assert body.ok is False
    dumped = body.model_dump()
    assert dumped["error"] == "timeout"


def test_session_sse_discriminator_roundtrip():
    ev = SessionPartialEvent(session_id="s1", stable_text="", tail_text="甚至")
    wrapped = SessionSSEEventEnvelope.model_validate(ev.model_dump())
    assert wrapped.root.event == "partial"
    assert wrapped.root.stable_text == ""
    assert wrapped.root.tail_text == "甚至"
    closed = SessionSSEEventEnvelope.model_validate(
        SessionClosedEvent(session_id="s1", reason=SessionClosedReason.cancelled).model_dump()
    )
    assert closed.root.reason == SessionClosedReason.cancelled


def test_session_partial_uses_stable_text_tail_text_only():
    ev = SessionPartialEvent(session_id="s1", stable_text="甚至出现", tail_text="交易")
    dumped = ev.model_dump()
    assert dumped["stable_text"] == "甚至出现"
    assert dumped["tail_text"] == "交易"
    assert "stable" not in dumped
    assert "tail" not in dumped
    assert "text" not in dumped
    with pytest.raises(ValidationError):
        SessionPartialEvent.model_validate(
            {"event": "partial", "session_id": "s1", "text": "甚至", "stable": "", "tail": "甚至"}
        )
    closed = SessionClosedEvent(session_id="s1", reason=SessionClosedReason.disconnected)
    assert closed.reason == SessionClosedReason.disconnected
    assert SessionClosedReason.disconnected.value == "disconnected"


def test_resend_needs_confirmation_ok_is_false():
    body = UtteranceResendNeedsConfirmation(original_job_id="old", new_job_id="new")
    assert body.ok is False
    assert body.requires_confirmation is True
    dumped = body.model_dump()
    assert dumped["ok"] is False
    with pytest.raises(ValidationError):
        UtteranceResendNeedsConfirmation.model_validate(
            {
                "ok": True,
                "requires_confirmation": True,
                "original_job_id": "old",
                "new_job_id": "new",
            }
        )


def test_source_includes_resend_and_ack_is_partial_or_unknown():
    rec = JobRecord(
        job_id="j-resend",
        state=JobState.queued,
        source=UtteranceSource.resend,
        focus_id="window-a",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    assert rec.source == UtteranceSource.resend
    ack = JobAckRequest(claim_id="c1", result=AckResult.partial_or_unknown)
    assert ack.result == AckResult.partial_or_unknown
    assert ack.result.value == "partial_or_unknown"
    with pytest.raises(ValidationError):
        JobAckRequest.model_validate({"claim_id": "c1", "result": "partial"})
    with pytest.raises(AttributeError):
        _ = AckResult.partial  # noqa: B018 — 不得保留旧别名


def test_job_query_unknown_is_not_a_persistable_state():
    found = JobQueryFound(
        job=JobRecord(
            job_id="j1",
            state=JobState.delivery_unknown,
            focus_id="window-a",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
    )
    assert found.found is True
    assert found.job.state == JobState.delivery_unknown
    unknown = JobQueryUnknown(job_id="missing")
    assert unknown.found is False
    assert unknown.state == "unknown"
    wrapped = JobQueryResponseEnvelope.model_validate(unknown.model_dump())
    assert wrapped.root.state == "unknown"
    with pytest.raises(ValidationError):
        JobRecord(
            job_id="j1",
            state="unknown",
            created_at="t",
            updated_at="t",
        )


def test_history_explicit_fields_and_legacy_mode_optional():
    legacy = HistoryRecord.model_validate(
        {
            "timestamp": "2026-01-01T10:00:00",
            "raw_text": "第一句",
            "final_text": "第一句",
            "mode": "raw",
            "language": "zh",
        }
    )
    assert legacy.mode == "raw"
    assert legacy.source is None
    assert legacy.polish is None
    assert legacy.job_id is None
    modern = HistoryRecord(
        timestamp="2026-01-01T12:00:00",
        raw_text="a",
        final_text="A",
        language="zh",
        source=UtteranceSource.resend,
        polish=HistoryPolish.failed,
        job_id="j9",
    )
    dumped = modern.model_dump()
    assert dumped["source"] == "resend"
    assert dumped["polish"] == "failed"
    assert dumped["job_id"] == "j9"
    assert "mode" in dumped
    assert dumped["mode"] is None
    without_mode = HistoryRecord.model_validate(
        {
            "timestamp": "t",
            "raw_text": "r",
            "final_text": "f",
            "language": "zh",
            "source": "desktop",
            "polish": "skipped",
            "job_id": "j2",
        }
    )
    assert without_mode.mode is None
    assert without_mode.polish == HistoryPolish.skipped
    with pytest.raises(ValidationError):
        HistoryRecord.model_validate(
            {
                "timestamp": "t",
                "raw_text": "r",
                "final_text": "f",
                "language": "zh",
                "polish": "raw",
            }
        )


def test_job_state_values_and_record_focus_is_per_job():
    assert JobState.failed.value == "failed"
    rec = JobRecord(
        job_id="j1",
        state=JobState.delivery_unknown,
        text="hi",
        focus_id="window-a",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    assert rec.focus_id == "window-a"
    assert "focus" not in rec.model_dump()
    with pytest.raises(ValidationError):
        JobRecord(
            job_id="j1",
            state="running",
            created_at="t",
            updated_at="t",
        )


def test_success_bodies_are_explicit_ok():
    assert SttResponse(text="a").ok is True
    assert PolishResponse(text="a").ok is True
    assert TranslateResponse(text="b", target_lang="en").ok is True


def test_product_events_are_typed_not_style_text():
    cases = [
        ProductIdleEvent(),
        ProductRecordingEvent(session_id="s"),
        ProductPartialEvent(session_id="s", stable_text="稳", tail_text="尾"),
        ProductPolishingEvent(job_id="j"),
        ProductTypedEvent(job_id="j", text="已输入"),
        ProductErrorEvent(error=ErrorCode.internal, message="boom"),
        ProductBusyEvent(),
        ProductHistoryWriteFailedEvent(job_id="j", message="disk", error=ErrorCode.history_io),
        ProductDeliveryUnknownEvent(job_id="j", text="maybe"),
    ]
    seen: set[str] = set()
    for ev in cases:
        wrapped = ProductSSEEventEnvelope.model_validate(ev.model_dump())
        assert wrapped.root.event == ev.event
        seen.add(ev.event.value if hasattr(ev.event, "value") else ev.event)
        assert not hasattr(ev, "style") or "style" not in ev.model_dump()
    for name in ProductEventType:
        assert name.value in seen, name
    partial = ProductPartialEvent(stable_text="a", tail_text="b")
    assert "stable_text" in partial.model_dump()
    assert "tail_text" in partial.model_dump()
    with pytest.raises(ValidationError):
        ProductSSEEventEnvelope.model_validate({"event": "status", "text": "idle", "style": "normal"})
    with pytest.raises(ValidationError):
        ProductPartialEvent.model_validate({"event": "partial", "text": "甚至", "stable": "", "tail": "甚至"})


def test_openapi_skeleton_includes_both_sse_families_and_no_fake_paths():
    spec = build_openapi_skeleton()
    assert spec["paths"] == {}
    names = set(spec["components"]["schemas"])
    for required in (
        "ErrorBody",
        "SessionPartialEvent",
        "SessionFinalEvent",
        "SessionErrorEvent",
        "SessionClosedEvent",
        "SessionSSEEventEnvelope",
        "ProductIdleEvent",
        "ProductRecordingEvent",
        "ProductPartialEvent",
        "ProductPolishingEvent",
        "ProductTypedEvent",
        "ProductErrorEvent",
        "ProductBusyEvent",
        "ProductHistoryWriteFailedEvent",
        "ProductDeliveryUnknownEvent",
        "ProductSSEEventEnvelope",
        "UtteranceResendNeedsConfirmation",
        "JobQueryFound",
        "JobQueryUnknown",
        "JobRecord",
        "HistoryRecord",
        "AppConfig",
    ):
        assert required in names, required
    assert "ProductStatusEvent" not in names
    for model in contract_models():
        assert model.__name__ in names
    session_props = spec["components"]["schemas"]["SessionPartialEvent"]["properties"]
    assert "stable_text" in session_props
    assert "tail_text" in session_props
    assert "stable" not in session_props
    assert "tail" not in session_props
    assert "text" not in session_props
    confirm = spec["components"]["schemas"]["UtteranceResendNeedsConfirmation"]["properties"]["ok"]
    # const false or enum [false]
    assert confirm.get("const") is False or confirm.get("enum") == [False] or confirm.get("default") is False
    history_props = spec["components"]["schemas"]["HistoryRecord"]["properties"]
    for key in ("source", "polish", "job_id", "mode"):
        assert key in history_props, key
    unknown_state = spec["components"]["schemas"]["JobQueryUnknown"]["properties"]["state"]
    assert unknown_state.get("const") == "unknown" or "unknown" in (unknown_state.get("enum") or [])
    job_state = spec["components"]["schemas"]["JobRecord"]["properties"]["state"]
    job_state_dump = str(job_state)
    assert "unknown" not in job_state_dump or job_state_dump.count("delivery_unknown")
    # JobRecord.state 不得把 unknown 列为可写取值
    ref = job_state.get("$ref")
    if ref:
        enum_name = ref.rsplit("/", 1)[-1]
        values = spec["components"]["schemas"][enum_name].get("enum") or []
        assert "unknown" not in values
        assert "delivery_unknown" in values


def test_streaming_blockers_remain_explicit():
    joined = " ".join(ASR_STREAMING_BLOCKERS)
    # 旧 P2 p95 阻塞已由 r9 证据关闭，不得残留；但限制必须如实保留，不得写成 ASR 全部已支持
    assert "1.515" not in joined and "不得宣称 sidecar 已可 120s 达标" not in joined
    assert any("suffix/prefix" in item or "拼接" in item for item in ASR_STREAMING_BLOCKERS)
    assert "用户口径" in joined and "0.749–1.226" in joined
    assert "不等于严格 p95≤1s" in joined and "超过 120s" in joined and "qwen3.5:4b" in joined
    assert "不承诺零错" in joined and "九点×21" in joined


def test_app_config_keeps_unknown_keys_and_defaults_to_qwen35():
    assert AppConfig().ollama_model == "qwen3.5:4b"
    cfg = AppConfig.model_validate({"ollama_model": "x", "future_flag": 1})
    assert cfg.ollama_model == "x"
    assert cfg.model_dump()["future_flag"] == 1


def test_resend_request_requires_original_job_id():
    req = UtteranceResendRequest(job_id="n", text="t", original_job_id="old")
    assert req.original_job_id == "old"
    with pytest.raises(ValidationError):
        UtteranceResendRequest(job_id="n", text="t")
    with pytest.raises(ValidationError):
        UtteranceResendRequest.model_validate({"job_id": "n", "text": "t", "original_job_id": ""})
