"""jobs 投递：claim → 按该 job 绑定的焦点注入 → ack；重复键走 resend，同一投递路径。

ack 只依据注入证据（见 `_inject`）：能证明打出 complete、能证明没打 none、否则 partial_or_unknown。
"""
from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from _contracts import ErrorCode
from _desktop.api import ApiError
from _desktop.status import PlatformStatus
from _focus import FocusProbe
from _inject import FocusBoundInjector, InjectEvidence


@dataclass(frozen=True)
class Delivered:
    job_id: str
    text: str


class LastDelivered:
    """最近一次被本机认领投递的 job（重复键的唯一来源；含焦点不符未输入的那句）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._item: Delivered | None = None

    def remember(self, job_id: str, text: str) -> None:
        with self._lock:
            self._item = Delivered(job_id, text)

    def get(self) -> Delivered | None:
        with self._lock:
            return self._item


class DeliveryLoop:
    def __init__(
        self,
        *,
        jobs,
        injector: FocusBoundInjector,
        last: LastDelivered,
        claim_timeout_s: float = 20.0,
        retry_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.jobs = jobs
        self.injector = injector
        self.last = last
        self.claim_timeout_s = claim_timeout_s
        self.retry_sleep = retry_sleep
        self._stop = threading.Event()

    def run_once(self) -> bool:
        """认领并处理至多一个 job。无 job 返回 False。"""
        job = self.jobs.claim(timeout_s=self.claim_timeout_s)
        if not job:
            return False
        job_id, claim_id, text = job["job_id"], job["claim_id"], job["text"]
        self.last.remember(job_id, text)
        try:
            result, evidence = self.injector.inject(text, focus_id=job.get("focus_id") or None, claim_id=claim_id)
        except Exception as exc:
            traceback.print_exc()
            result = "partial_or_unknown"
            evidence = InjectEvidence(False, False, False, method="error", detail=f"注入异常：{str(exc)[:80]}")
            self.injector.acks.record(claim_id, result)
        print(f"  投递: {job_id} → {result} ({evidence.method}: {evidence.detail})", flush=True)
        self.jobs.ack(job_id, claim_id, result, detail=evidence.detail)
        return True

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                print(f"  投递: 认领异常 {exc}, 3s 后重试", flush=True)
                self.retry_sleep(3.0)

    def stop(self) -> None:
        self._stop.set()


class RepeatAction:
    """重复键：把最近一次投递的文本作为新 job 重发到按键时的焦点（不是原焦点）。

    用户按重复键即明确要再输入一次，故对 delivery_unknown 原 job 直接带 confirm。
    """

    def __init__(
        self,
        *,
        api,
        last: LastDelivered,
        probe: FocusProbe,
        status: PlatformStatus,
        job_ids: Callable[[], str],
    ) -> None:
        self.api = api
        self.last = last
        self.probe = probe
        self.status = status
        self.job_ids = job_ids

    def run(self) -> None:
        item = self.last.get()
        if item is None:
            self.status.error(ErrorCode.invalid_request, "还没有识别记录")
            return
        focus = self.probe.current_id()
        try:
            self.api.resend(
                job_id=self.job_ids(),
                text=item.text,
                original_job_id=item.job_id,
                focus_id=str(focus) if focus else "",
                confirm=True,
            )
        except ApiError as exc:
            self.status.error(ErrorCode.invalid_request if exc.status < 500 else ErrorCode.internal, f"重复输入失败：{exc.message}")
        except Exception as exc:
            self.status.error(ErrorCode.internal, f"重复输入失败：{str(exc)[:80]}")
