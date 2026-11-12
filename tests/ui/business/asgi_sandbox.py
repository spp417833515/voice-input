#!/usr/bin/env python3
"""临时起一个隔离的 v3 ASGI 沙箱，监听 127.0.0.1 随机端口（禁用 17945/11434）。不改服务源码。

ready / meta 只在 uvicorn 确认已监听之后写入，避免调用方在端口尚未 accept 时就开始 fetch。
注入测试用 setup/lan 替身，不探测本机真实环境、不启动官方软件、不安装 qrcode。
挂载 static/v3，使 /setup /voice /openapi.snapshot.json 走真实新路由。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import tempfile
from pathlib import Path
from threading import Lock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

RESERVED = {17945, 11434}


def _pick_port() -> int:
    for _ in range(20):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        if port not in RESERVED:
            return port
    raise RuntimeError("无法分配非保留端口")


class SandboxTasks:
    """内存任务表：同 kind 在 running 时幂等返回，轮询推进进度后 succeeded。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._tasks: dict[str, dict] = {}
        self._by_kind: dict[str, str] = {}
        self._seq = 0

    def _public(self, task: dict) -> dict:
        return {
            "task_id": task["task_id"],
            "kind": task["kind"],
            "state": task["state"],
            "progress": task.get("progress"),
            "message": task.get("message") or "",
            "error": task.get("error"),
            "started_at": task["started_at"],
            "finished_at": task.get("finished_at"),
        }

    def start(self, kind):
        from _contracts import SetupTaskKind

        kind_value = kind.value if isinstance(kind, SetupTaskKind) else str(kind)
        with self._lock:
            existing_id = self._by_kind.get(kind_value)
            if existing_id and self._tasks[existing_id]["state"] == "running":
                return self._public(self._tasks[existing_id])
            self._seq += 1
            task_id = "sandbox-task-" + str(self._seq)
            task = {
                "task_id": task_id,
                "kind": kind_value,
                "state": "running",
                "progress": {
                    "completed_bytes": 0,
                    "total_bytes": 100,
                    "percent": 0,
                    "stage": "start",
                },
                "message": "sandbox running",
                "error": None,
                "started_at": "2026-11-10T00:00:00+00:00",
                "finished_at": None,
                "_ticks": 0,
            }
            self._tasks[task_id] = task
            self._by_kind[kind_value] = task_id
            return self._public(task)

    def get(self, task_id: str):
        from _contracts import ErrorCode, LogicError

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise LogicError(ErrorCode.not_found, "没有这个任务")
            if task["state"] == "running":
                task["_ticks"] += 1
                percent = min(100, task["_ticks"] * 25)
                task["progress"] = {
                    "completed_bytes": percent,
                    "total_bytes": 100,
                    "percent": percent,
                    "stage": "download",
                }
                task["message"] = "sandbox running"
                if task["_ticks"] >= 2:
                    task["state"] = "succeeded"
                    task["finished_at"] = "2026-11-10T00:00:01+00:00"
                    task["message"] = "sandbox completed"
                    task["progress"] = {
                        "completed_bytes": 100,
                        "total_bytes": 100,
                        "percent": 100,
                        "stage": "done",
                    }
                    self._status_after_success(task["kind"])
            return self._public(task)

    def _status_after_success(self, kind: str) -> None:
        # 由 Setup 替身在 check() 里读取 _done kinds
        return


class SandboxSetup:
    def __init__(self) -> None:
        self.tasks = SandboxTasks()
        self._done: set[str] = set()

    def check(self):
        done = set()
        with self.tasks._lock:
            for kind, task_id in self.tasks._by_kind.items():
                task = self.tasks._tasks.get(task_id)
                if task and task["state"] == "succeeded":
                    done.add(kind)
        asr_ready = "asr_download" in done and "asr_start" in done
        asr_downloading = any(
            self._running(k) for k in ("asr_download", "asr_start")
        )
        if asr_ready:
            asr_status = "ready"
            asr_action = None
        elif asr_downloading:
            asr_status = "downloading" if self._running("asr_download") else "missing"
            asr_action = "asr_download" if self._running("asr_download") else "asr_start"
        elif "asr_download" in done:
            asr_status = "missing"
            asr_action = "asr_start"
        else:
            asr_status = "missing"
            asr_action = "asr_download"
        running_asr = self._running_task("asr_download") or self._running_task("asr_start")

        ollama_installed = "ollama_install" in done
        ollama_running = "ollama_start" in done
        model_ready = "ollama_pull" in done
        if self._running("ollama_pull"):
            model_status = "downloading"
            ol_action = "ollama_pull"
        elif model_ready:
            model_status = "ready"
            ol_action = None if ollama_running else "ollama_start"
        elif not ollama_installed:
            model_status = "missing"
            ol_action = "ollama_install"
        elif not ollama_running:
            model_status = "missing"
            ol_action = "ollama_start"
        else:
            model_status = "missing"
            ol_action = "ollama_pull"
        running_ol = (
            self._running_task("ollama_install")
            or self._running_task("ollama_start")
            or self._running_task("ollama_pull")
        )
        return {
            "ok": True,
            "session_type": "sandbox",
            "asr": {
                "model_name": "Qwen/Qwen3-ASR-1.7B",
                "status": asr_status,
                "weights_present": "asr_download" in done,
                "sidecar_running": "asr_start" in done,
                "size_label": None,
                "device_label": None,
                "progress": (running_asr or {}).get("progress") if asr_status == "downloading" else None,
                "message": "沙箱探测：未接生产设备" if asr_status != "ready" else "沙箱识别已就绪",
                "action": asr_action,
                "task_id": (running_asr or {}).get("task_id"),
            },
            "ollama": {
                "model_name": "qwen3.5:4b",
                "installed": ollama_installed,
                "version": "sandbox" if ollama_installed else None,
                "running": ollama_running,
                "model_status": model_status,
                "size_label": None,
                "progress": (running_ol or {}).get("progress") if model_status == "downloading" else None,
                "message": "沙箱探测：未接生产设备" if model_status != "ready" else "沙箱模型已就绪",
                "action": ol_action,
                "task_id": (running_ol or {}).get("task_id"),
            },
            "capabilities": [
                {
                    "id": "clipboard",
                    "label": "剪贴板",
                    "available": False,
                    "required": True,
                    "detail": "沙箱未提供剪贴板",
                }
            ],
            "unmet_gates": ["sandbox runtime: 非生产，未切流"],
        }

    def _running(self, kind: str) -> bool:
        task = self._running_task(kind)
        return bool(task)

    def _running_task(self, kind: str):
        with self.tasks._lock:
            task_id = self.tasks._by_kind.get(kind)
            if not task_id:
                return None
            task = self.tasks._tasks.get(task_id)
            if task and task["state"] == "running":
                return task
            return None


class SandboxLan:
    """不调用 qrcode，返回固定测试网地址与遮罩。"""

    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token

    def describe(self):
        masked = (self.token[:3] + "****" + self.token[-3:]) if len(self.token) > 6 else "****"
        url = "https://192.0.2.8:" + str(self.port) + "/voice?token=" + self.token
        url_masked = "https://192.0.2.8:" + str(self.port) + "/voice?token=" + masked
        qr_svg = "<svg xmlns='http://www.w3.org/2000/svg' width='8' height='8'></svg>"
        return {
            "port": self.port,
            "token": self.token,
            "token_masked": masked,
            "entries": [
                {
                    "ip": "192.0.2.8",
                    "url": url,
                    "url_masked": url_masked,
                    "qr_svg": qr_svg,
                }
            ],
            "message": "",
        }


def main() -> int:
    meta_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / "v3-asgi-meta.json"
    sandbox_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp(prefix="v3-ui-asgi-"))
    sandbox_root.mkdir(parents=True, exist_ok=True)

    from _contracts import SttResponse
    from _v3.runtime import build_sandbox_runtime
    from _v3_app import create_app
    import uvicorn

    port = _pick_port()
    if port in RESERVED:
        raise SystemExit("拒绝使用保留端口")

    def transcribe(audio: bytes, language=None, context=None):
        return SttResponse(text="sandbox transcript", language=language or "zh")

    token = "ui-business-sandbox-token"
    setup = SandboxSetup()
    runtime = build_sandbox_runtime(
        root=sandbox_root,
        token=token,
        port=port,
        transcribe=transcribe,
        setup=setup,
        unmet_gates=("sandbox runtime: 非生产，未切流",),
    )
    runtime.lan = SandboxLan(port, token)
    static_v3 = REPO / "static" / "v3"
    app = create_app(runtime, static_v3=static_v3)
    meta = {
        "host": "127.0.0.1",
        "port": port,
        "pid": os.getpid(),
        "root": str(sandbox_root),
        "ready": False,
    }

    def write_ready() -> None:
        meta["ready"] = True
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

    class ListeningServer(uvicorn.Server):
        """startup 在 create_server 成功并把 self.started 置 True 之后才回调。"""

        def __init__(self, config: uvicorn.Config, on_listening):
            super().__init__(config)
            self._on_listening = on_listening

        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets=sockets)
            if not self.started:
                raise RuntimeError("uvicorn 启动完成但未进入监听，拒绝写出 ready/meta")
            self._on_listening()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = ListeningServer(config, write_ready)

    def stop(*_args):
        server.should_exit = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.run()
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit("ASGI 沙箱启动失败：" + str(exc)) from exc
    if not meta["ready"]:
        raise SystemExit("ASGI 沙箱在未进入监听的情况下结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
