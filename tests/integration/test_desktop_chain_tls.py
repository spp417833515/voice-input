"""§8.2-6 桌面全链路：真实 TLS（随机回环端口 + 临时自签证书，校验开启）上的
按住说话 → 识别会话 → utterance → jobs 认领 → 按 job 焦点注入 → ack → 产品事件；重复键 resend；焦点不符。

不碰真实麦克风/键鼠/剪贴板/窗口：FakeAudioSource 合成 PCM、StaticFocusProbe、RecordingBackend。
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from _capture import FakeAudioSource
from _contracts import JobState
from _desktop.api import DesktopApi
from _desktop.delivery import DeliveryLoop, LastDelivered, RepeatAction
from _desktop.status import PlatformStatus
from _desktop.voice import VoiceFlow
from _focus import StaticFocusProbe
from _inject import FocusBoundInjector, RecordingBackend
from _v3.isolated import FORBIDDEN_PORTS, compose_isolated_candidate
from tests.integration.fixtures import FixtureStreamProvider, fixture_transcribe

TOKEN = "test-desktop-chain-token"
PCM_1S = b"\x01\x00" * 16000


def _self_signed(tmp: Path) -> tuple[Path, Path]:
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return cert, key


class Bus:
    def __init__(self, runtime) -> None:
        self.events: list[dict] = []
        self._lock = threading.Lock()
        runtime.events.subscribe(self._on)

    def _on(self, event) -> None:
        with self._lock:
            self.events.append(event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event))

    def kinds(self) -> list[str]:
        with self._lock:
            return [e["event"] for e in self.events]

    def wait_for(self, predicate, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for e in self.events:
                    if predicate(e):
                        return e
            time.sleep(0.02)
        raise AssertionError(f"事件未出现，现有 {self.kinds()}")


@pytest.fixture
def chain(tmp_path: Path):
    pytest.importorskip("uvicorn")
    cert, key = _self_signed(tmp_path)
    comp = compose_isolated_candidate(
        root=tmp_path / "isolated",
        token=TOKEN,
        cert=cert,
        key=key,
        transcribe=fixture_transcribe,
        stream_provider=FixtureStreamProvider(final_text="桌面定稿"),
        mount_static=False,
        fixture_notes=("desktop chain: FixtureStreamProvider，非官方引擎",),
    )
    assert comp.bind_port not in FORBIDDEN_PORTS
    base = comp.start_loopback_https()
    api = DesktopApi(base, cert=cert)
    try:
        yield comp, api, Bus(comp.runtime)
    finally:
        api.close()
        comp.close()


def _speak(api, status, focus: int) -> str:
    source = FakeAudioSource(PCM_1S)
    flow = VoiceFlow(api=api, status=status, probe=StaticFocusProbe(focus), source_factory=lambda: source)
    assert flow.start() is True
    flow.stop()
    upload = flow.last_upload
    upload.join(timeout=10)
    assert upload.outcome == "submitted", upload.outcome
    return upload.job_id


def test_tls_verification_is_on(chain, tmp_path):
    comp, _api, _bus = chain
    (tmp_path / "other").mkdir()
    other_cert, _ = _self_signed(tmp_path / "other")
    wrong = DesktopApi(comp.base_url(), cert=other_cert)
    try:
        with pytest.raises(Exception):
            wrong.get_config()
    finally:
        wrong.close()


def test_voice_to_delivery_complete_with_product_events(chain):
    comp, api, bus = chain
    status = PlatformStatus(api.post_platform_event)
    job_id = _speak(api, status, focus=7)

    backend = RecordingBackend()
    last = LastDelivered()
    loop = DeliveryLoop(jobs=api.jobs, injector=FocusBoundInjector(StaticFocusProbe(7), backend), last=last, claim_timeout_s=3)
    assert loop.run_once() is True
    assert backend.sent and "桌面定稿" in backend.sent[0]
    record = api.jobs.get_job(job_id)
    assert record["job"]["state"] == JobState.done
    assert last.get().job_id == job_id

    bus.wait_for(lambda e: e["event"] == "typed" and e.get("job_id") == job_id)
    kinds = bus.kinds()
    assert "recording" in kinds and "partial" in kinds, kinds
    assert kinds.index("recording") < kinds.index("partial") < kinds.index("typed")
    status.close()


def test_focus_mismatch_acks_none_and_shows_delivery_failed(chain):
    comp, api, bus = chain
    status = PlatformStatus(api.post_platform_event)
    job_id = _speak(api, status, focus=7)
    backend = RecordingBackend()
    loop = DeliveryLoop(jobs=api.jobs, injector=FocusBoundInjector(StaticFocusProbe(9), backend), last=LastDelivered(), claim_timeout_s=3)
    assert loop.run_once() is True
    assert backend.sent == []
    assert api.jobs.get_job(job_id)["job"]["state"] == JobState.failed
    err = bus.wait_for(lambda e: e["event"] == "error" and e.get("error") == "delivery_failed")
    assert err["details"]["job_id"] == job_id
    status.close()


def test_repeat_resends_to_current_focus(chain):
    comp, api, bus = chain
    status = PlatformStatus(api.post_platform_event)
    first = _speak(api, status, focus=7)
    last = LastDelivered()
    backend = RecordingBackend()
    DeliveryLoop(jobs=api.jobs, injector=FocusBoundInjector(StaticFocusProbe(7), backend), last=last, claim_timeout_s=3).run_once()

    ids = iter(["desk-repeat-1"])
    RepeatAction(api=api, last=last, probe=StaticFocusProbe(8), status=status, job_ids=lambda: next(ids)).run()
    repeat_job = api.jobs.get_job("desk-repeat-1")["job"]
    assert repeat_job["focus_id"] == "8"
    assert repeat_job["state"] == JobState.queued

    backend2 = RecordingBackend()
    DeliveryLoop(jobs=api.jobs, injector=FocusBoundInjector(StaticFocusProbe(8), backend2), last=last, claim_timeout_s=3).run_once()
    assert backend2.sent == backend.sent
    assert api.jobs.get_job("desk-repeat-1")["job"]["state"] == JobState.done
    assert last.get().job_id == "desk-repeat-1"
    assert first != "desk-repeat-1"
    status.close()


def test_platform_status_reaches_product_stream(chain):
    comp, api, bus = chain
    status = PlatformStatus(api.post_platform_event)
    status.busy()
    from _contracts import ErrorCode

    status.error(ErrorCode.dependency_failed, "麦克风打开失败：fake")
    status.drain()
    bus.wait_for(lambda e: e["event"] == "busy")
    bus.wait_for(lambda e: e["event"] == "error" and e["message"].startswith("麦克风"))
    status.close()


def test_too_short_cancels_session_without_job(chain):
    comp, api, bus = chain
    status = PlatformStatus(api.post_platform_event)
    source = FakeAudioSource(b"\x00\x00" * 800)
    flow = VoiceFlow(api=api, status=status, probe=StaticFocusProbe(7), source_factory=lambda: source)
    flow.start()
    flow.stop()
    flow.last_upload.join(timeout=10)
    assert flow.last_upload.outcome == "cancelled"
    assert api.jobs.claim(timeout_s=0) is None
    bus.wait_for(lambda e: e["event"] == "idle")
    status.close()
