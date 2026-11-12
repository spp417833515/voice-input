from __future__ import annotations

import pytest

from _focus import StaticFocusProbe, decide_focus
from _inject import ClaimAckLog, FocusBoundInjector, RecordingBackend, ack_from_evidence, InjectEvidence
from _overlay import InjectFocusGuard, is_allowed_ui_url, STATUS_WINDOW_POLICY


def test_focus_match_mismatch_unknown():
    assert decide_focus(12, 12) == "match"
    assert decide_focus(12, 13) == "mismatch"
    assert decide_focus(12, 0) == "unknown"
    assert decide_focus(None, 12) == "unknown"


def test_inject_requires_matching_focus_and_ack_evidence():
    probe = StaticFocusProbe(focus_id=7)
    backend = RecordingBackend()
    inj = FocusBoundInjector(probe, backend)
    result, evidence = inj.inject("hello", focus_id=7, claim_id="c1")
    assert result == "complete"
    assert backend.sent == ["hello"]
    result2, evidence2 = inj.inject("nope", focus_id=8, claim_id="c2")
    assert result2 == "none"
    assert evidence2.proved_none is True
    assert backend.sent == ["hello"]
    unknown = FocusBoundInjector(StaticFocusProbe(0), RecordingBackend()).inject("x", focus_id=7)
    # 焦点未知且假后端证明了输出 → complete；改为无法证明时才 unknown
    assert unknown[0] in {"complete", "partial_or_unknown"}


def test_unknown_output_is_not_failed():
    ev = InjectEvidence(False, False, False, method="ibus")
    assert ack_from_evidence(ev) == "partial_or_unknown"
    log = ClaimAckLog()
    assert log.record("c", "partial_or_unknown") == "partial_or_unknown"
    assert log.record("c", "partial_or_unknown") == "partial_or_unknown"
    with pytest.raises(ValueError):
        log.record("c", "complete")


def test_translation_copy_does_not_change_bound_focus():
    guard = InjectFocusGuard()
    guard.bind(42)
    guard.copy_translation("译文")
    assert guard.inject_focus_id() == 42
    assert guard.copied_text == "译文"


def test_ui_url_and_status_policy():
    assert is_allowed_ui_url("https://127.0.0.1:17945/voice")
    assert not is_allowed_ui_url("file:///tmp/x.html")
    assert not is_allowed_ui_url("https://example.com/")
    assert not is_allowed_ui_url("http://127.0.0.1:17945/")
    assert STATUS_WINDOW_POLICY.accept_focus is False
    assert STATUS_WINDOW_POLICY.keep_above is True
