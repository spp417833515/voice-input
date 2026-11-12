"""注入链。只负责把已定稿文本送到已绑定焦点；不 polish、不写历史、不入队。

ACK 只能依据可证明的输出证据：
- complete：能证明文字已输出
- none：能证明这次没有输出
- partial_or_unknown：无法证明未输出，禁止改写成 failed，禁止自动重试
同 claim_id 重复上报同一结果视为幂等；不同结果视为冲突可见。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from _focus import FocusDecision, FocusProbe, decide_focus

AckResult = Literal["complete", "none", "partial_or_unknown"]


@dataclass(frozen=True)
class InjectEvidence:
    delivered: bool
    proved_output: bool
    proved_none: bool
    method: str
    detail: str = ""
    focus_decision: FocusDecision = "unknown"


class InjectBackend(Protocol):
    def deliver(self, text: str, *, terminal: bool = False) -> InjectEvidence: ...


class RecordingBackend:
    """测试用。不向真实窗口注入，不读剪贴板。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail: str | None = None
        self.ambiguous: bool = False

    def deliver(self, text: str, *, terminal: bool = False) -> InjectEvidence:
        del terminal
        if self.fail == "none":
            return InjectEvidence(False, False, True, method="fake", detail="refused")
        if self.fail == "unknown" or self.ambiguous:
            self.sent.append(text)
            return InjectEvidence(False, False, False, method="fake", detail="unknown")
        self.sent.append(text)
        return InjectEvidence(True, True, False, method="fake", detail="recorded")


def ack_from_evidence(evidence: InjectEvidence) -> AckResult:
    if evidence.proved_output:
        return "complete"
    if evidence.proved_none:
        return "none"
    return "partial_or_unknown"


class ClaimAckLog:
    """客户端同 claim 上报记录。幂等只接受相同结果。"""

    def __init__(self) -> None:
        self._items: dict[str, AckResult] = {}

    def record(self, claim_id: str, result: AckResult) -> AckResult:
        if not claim_id:
            raise ValueError("claim_id 不能为空")
        previous = self._items.get(claim_id)
        if previous is None:
            self._items[claim_id] = result
            return result
        if previous != result:
            raise ValueError(f"同一 claim_id 已上报 {previous}，不能改为 {result}")
        return previous

    def get(self, claim_id: str) -> AckResult | None:
        return self._items.get(claim_id)


class FocusBoundInjector:
    def __init__(self, probe: FocusProbe, backend: InjectBackend, acks: ClaimAckLog | None = None) -> None:
        self.probe = probe
        self.backend = backend
        self.acks = acks or ClaimAckLog()

    def inject(self, text: str, *, focus_id: int | str | None, claim_id: str | None = None) -> tuple[AckResult, InjectEvidence]:
        decision = decide_focus(focus_id, self.probe.current_id())
        if decision == "mismatch":
            evidence = InjectEvidence(
                delivered=False,
                proved_output=False,
                proved_none=True,
                method="skipped",
                detail="焦点与任务绑定时不一致，未注入",
                focus_decision=decision,
            )
        else:
            evidence = self.backend.deliver(text, terminal=self.probe.is_terminal())
            evidence = InjectEvidence(
                delivered=evidence.delivered,
                proved_output=evidence.proved_output,
                proved_none=evidence.proved_none,
                method=evidence.method,
                detail=evidence.detail,
                focus_decision=decision,
            )
            if decision == "unknown" and evidence.proved_output is False and evidence.proved_none is False:
                evidence = InjectEvidence(
                    delivered=evidence.delivered,
                    proved_output=False,
                    proved_none=False,
                    method=evidence.method,
                    detail="焦点未知且无法证明输出",
                    focus_decision=decision,
                )
        result = ack_from_evidence(evidence)
        if claim_id:
            self.acks.record(claim_id, result)
        return result, evidence
