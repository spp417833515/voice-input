"""真实动作/探测的协议与失败路径：全部用 httpx.MockTransport、假进程、临时目录，不触网、不调 live systemctl。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
from pathlib import Path

import httpx
import pytest

from _contracts import LogicError, SetupProgress
from _v3.lan_access import list_physical_lan_ipv4, load_or_create_token
from _v3.setup_actions import (
    asr_download_action,
    ollama_install_action,
    ollama_pull_action,
)
from _v3.setup_probe import probe_asr_sidecar, probe_asr_weights, probe_ollama


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://ollama.test", transport=httpx.MockTransport(handler))


def _pull_handler(lines: list[dict], *, tags: list[str], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/pull":
            body = json.loads(request.content)
            assert body == {"model": "qwen3.5:4b", "stream": True}
            payload = "\n".join(json.dumps(item) for item in lines) + "\n"
            return httpx.Response(status, content=payload.encode())
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n, "model": n} for n in tags]})
        return httpx.Response(404)

    return handler


def test_ollama_pull_reports_aggregated_progress_and_verifies_tags():
    seen: list[SetupProgress] = []
    lines = [
        {"status": "pulling manifest"},
        {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 50},
        {"status": "pulling b", "digest": "sha256:b", "total": 300, "completed": 0},
        {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 100},
        {"status": "pulling b", "digest": "sha256:b", "total": 300, "completed": 300},
        {"status": "verifying sha256 digest"},
        {"status": "success"},
    ]
    action = ollama_pull_action(client_factory=lambda: _client(_pull_handler(lines, tags=["qwen3.5:4b"])), model="qwen3.5:4b")
    assert "qwen3.5:4b" in action(seen.append)
    assert seen[3].completed_bytes == 100 and seen[3].total_bytes == 400 and seen[3].percent == 25.0
    assert seen[-1].stage == "success" and seen[-1].percent == 100.0


@pytest.mark.parametrize(
    "lines,tags,status,needle",
    [
        ([{"status": "pulling manifest"}, {"error": "pull model manifest: 412"}], [], 200, "412"),
        ([{"error": "boom"}], [], 500, "HTTP 500"),
        ([{"status": "pulling manifest"}], [], 200, "未完成"),
        ([{"status": "success"}], ["other:1b"], 200, "/api/tags"),
    ],
)
def test_ollama_pull_failures_are_visible(lines, tags, status, needle):
    action = ollama_pull_action(client_factory=lambda: _client(_pull_handler(lines, tags=tags, status=status)), model="qwen3.5:4b")
    with pytest.raises(LogicError) as ei:
        action(lambda _p: None)
    assert needle in ei.value.message


def test_ollama_pull_connection_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LogicError) as ei:
        ollama_pull_action(client_factory=lambda: _client(handler), model="qwen3.5:4b")(lambda _p: None)
    assert "无法连接" in ei.value.message


class FakeRun:
    def __init__(self, returncode=0, stderr="", effect=None):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.effect = effect

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        if self.effect:
            self.effect(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr=self.stderr)


def _release_handler(blob: bytes, digest: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sha256sum.txt"):
            return httpx.Response(200, text=f"{digest}  ./ollama-linux-amd64.tar.zst\nffff  ./other.tgz\n")
        if request.url.path.endswith("/ollama-linux-amd64.tar.zst"):
            assert "/v0.40.16/" in request.url.path
            return httpx.Response(200, content=blob, headers={"content-length": str(len(blob))})
        return httpx.Response(404)

    return handler


def test_ollama_install_verifies_sha_and_extracts_to_user_dir(tmp_path):
    blob = b"fake-tar-zst-bytes" * 100
    digest = hashlib.sha256(blob).hexdigest()

    def extract(cmd):
        staging = Path(cmd[cmd.index("-C") + 1])
        (staging / "bin").mkdir(parents=True)
        (staging / "bin" / "ollama").write_text("#!/bin/sh\n")

    run = FakeRun(effect=extract)
    install_dir = tmp_path / "ollama-bin"
    progress: list[SetupProgress] = []
    action = ollama_install_action(version="0.40.16", install_dir=install_dir,
                                   client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_release_handler(blob, digest))),
                                   run=run)
    assert "已安装" in action(progress.append)
    assert (install_dir / "bin" / "ollama").is_file()
    assert run.calls[0][:3] == ["tar", "--zstd", "-xf"]
    assert progress[-2].percent == 100.0
    assert not list(tmp_path.glob(".*download")) and not list(tmp_path.glob(".*staging"))
    with pytest.raises(LogicError):
        action(lambda _p: None)


def test_ollama_install_rejects_checksum_mismatch(tmp_path):
    run = FakeRun()
    action = ollama_install_action(version="0.40.16", install_dir=tmp_path / "ob",
                                   client_factory=lambda: httpx.Client(transport=httpx.MockTransport(_release_handler(b"x", "0" * 64))),
                                   run=run)
    with pytest.raises(LogicError) as ei:
        action(lambda _p: None)
    assert "sha256" in ei.value.message
    assert run.calls == [] and not (tmp_path / "ob").exists()


class FakePopen:
    def __init__(self, *, model_dir: Path, returncode: int, write_weights: bool, stderr_text: str = ""):
        self.model_dir = model_dir
        self._rc = returncode
        self.write_weights = write_weights
        self.stderr_text = stderr_text
        self.polls = 0
        self.args = None

    def __call__(self, args, **kw):
        self.args = args
        kw["stderr"].write(self.stderr_text)
        self.stdout = io.StringIO(json.dumps({"total_bytes": 1000}) + "\n")
        return self

    def poll(self):
        self.polls += 1
        if self.polls < 3:
            return None
        if self.write_weights:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            (self.model_dir / "config.json").write_text("{}")
            (self.model_dir / "model.safetensors").write_bytes(b"0" * 10)
        self.returncode = self._rc
        return self._rc


def test_asr_download_runs_worker_in_isolated_env_and_rechecks(tmp_path):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    model_dir = tmp_path / "models" / "Qwen3-ASR-1.7B"
    popen = FakePopen(model_dir=model_dir, returncode=0, write_weights=True)
    progress: list[SetupProgress] = []
    action = asr_download_action(python=python, worker=Path("/w.py"), repo_id="Qwen/Qwen3-ASR-1.7B",
                                 model_dir=model_dir, log_path=tmp_path / "log" / "a.log",
                                 env={"HF_HOME": str(tmp_path / "hf")}, popen=popen, sleep=lambda _s: None)
    assert "已下载" in action(progress.append)
    assert popen.args == [str(python), "/w.py", "Qwen/Qwen3-ASR-1.7B", str(model_dir)]
    assert progress and progress[0].stage == "下载识别模型权重"


def test_asr_download_failures_visible(tmp_path):
    model_dir = tmp_path / "m"
    missing_env = asr_download_action(python=tmp_path / "nope", worker=Path("/w.py"), repo_id="r", model_dir=model_dir,
                                      log_path=tmp_path / "l.log", env={}, popen=FakePopen(model_dir=model_dir, returncode=0, write_weights=True))
    with pytest.raises(LogicError) as ei:
        missing_env(lambda _p: None)
    assert "未安装" in ei.value.message
    python = tmp_path / "py"
    python.write_text("")
    failing = asr_download_action(python=python, worker=Path("/w.py"), repo_id="r", model_dir=model_dir,
                                  log_path=tmp_path / "l.log", env={}, sleep=lambda _s: None,
                                  popen=FakePopen(model_dir=model_dir, returncode=1, write_weights=False, stderr_text="HTTP 403 fixture"))
    with pytest.raises(LogicError) as ei:
        failing(lambda _p: None)
    assert "403 fixture" in ei.value.message
    incomplete = asr_download_action(python=python, worker=Path("/w.py"), repo_id="r", model_dir=model_dir,
                                     log_path=tmp_path / "l.log", env={}, sleep=lambda _s: None,
                                     popen=FakePopen(model_dir=model_dir, returncode=0, write_weights=False))
    with pytest.raises(LogicError) as ei:
        incomplete(lambda _p: None)
    assert "不完整" in ei.value.message


def test_probe_asr_weights_complete_and_partial(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"a": "s1.safetensors", "b": "s2.safetensors"}}))
    (d / "s1.safetensors").write_bytes(b"1")
    probe = probe_asr_weights(d)
    assert not probe.complete and "s2.safetensors" in probe.missing
    (d / "s2.safetensors").write_bytes(b"2")
    assert probe_asr_weights(d).complete
    (d / ".cache").mkdir()
    (d / ".cache" / "x.incomplete").write_bytes(b"")
    assert not probe_asr_weights(d).complete
    assert not probe_asr_weights(tmp_path / "none").complete


def test_probe_ollama_and_sidecar(tmp_path):
    binary = tmp_path / "ollama"
    binary.write_text("")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.16"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:4b", "size": 3_400_000_000}]})
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "loaded": True})
        return httpx.Response(404)

    with _client(handler) as client:
        probe = probe_ollama(binary=binary, client=client, model="qwen3.5:4b")
        assert probe.installed and probe.running and probe.has_model and probe.version == "0.40.16"
        side = probe_asr_sidecar(client)
        assert side.running and side.loaded

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    with _client(down) as client:
        probe = probe_ollama(binary=tmp_path / "missing", client=client, model="qwen3.5:4b")
        assert not probe.installed and not probe.running and "refused" in probe.error
        assert not probe_asr_sidecar(client).running


def test_lan_ip_listing_skips_virtual_bridges(tmp_path):
    for name in ("wlp1s0", "eth0"):
        (tmp_path / name / "device").mkdir(parents=True)
    payload = [
        {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
        {"ifname": "wlp1s0", "addr_info": [{"local": "10.0.0.110"}]},
        {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1"}]},
        {"ifname": "eth0", "addr_info": [{"local": "198.18.0.5"}, {"local": "192.168.1.9"}, {"local": "169.254.1.1"}]},
    ]
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
    assert list_physical_lan_ipv4(run=run, sys_class_net=tmp_path) == ["10.0.0.110", "192.168.1.9"]
    bad = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no ip")
    with pytest.raises(LogicError):
        list_physical_lan_ipv4(run=bad, sys_class_net=tmp_path)


def test_token_file_created_0600_and_stable(tmp_path):
    path = tmp_path / "token"
    first = load_or_create_token(path)
    assert len(first) >= 24
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_or_create_token(path) == first
