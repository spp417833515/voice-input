"""L4：ASR 新启动前显存预检（假 nvidia-smi / 假 systemctl；不碰 GPU、不导入 torch/vllm）。

比较式与 venv-asr 内 vLLM 0.14.0 同源：vllm/v1/worker/utils.py:253-273 request_memory
（requested = ceil(total × gpu_memory_utilization)，free < requested 即拒启），由 gpu_worker.py:233-234 调用。
"""
from __future__ import annotations

import math
import subprocess
import sys

import pytest

from _v3.gpu_preflight import asr_gpu_preflight, asr_gpu_preflight_for_unit, asr_utilization_from_argv, main
from runtimes.asr.protocol import DEFAULT_GPU_MEMORY_UTILIZATION

ARGV = ("/v/bin/python", "-m", "runtimes.asr", "serve", "--host", "127.0.0.1", "--port", "17946", "--load", "--warmup", "--model", "/m/Qwen3-ASR-1.7B")
WHICH = lambda n: f"/usr/bin/{n}"  # noqa: E731


def smi(out: str, rc: int = 0, calls: list | None = None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        assert cmd[0] == "nvidia-smi"
        return subprocess.CompletedProcess(cmd, rc, out, "" if rc == 0 else "NVIDIA-SMI has failed")

    return run


def one_gpu(free: int, total: int = 16303) -> str:
    return f"0, GPU-f7da, {total}, {free}, NVIDIA GeForce RTX 5070 Ti\n"


def test_default_utilization_comes_from_sidecar_parser_and_protocol_constant():
    assert asr_utilization_from_argv(ARGV) == DEFAULT_GPU_MEMORY_UTILIZATION
    assert asr_utilization_from_argv((*ARGV, "--gpu-memory-utilization", "0.55")) == 0.55


@pytest.mark.parametrize("argv", [("/usr/bin/ollama", "serve"), ("/v/bin/python", "-m", "runtimes.asr", "serve", "--bogus")])
def test_unrecognised_exec_start_is_unknown(argv):
    result = asr_gpu_preflight(argv, run=smi(one_gpu(16000)), which=WHICH)
    assert result.kind == "unknown" and not result.ok


def test_enough_and_boundary_follow_vllm_ceil_comparison():
    required = math.ceil(16303 * DEFAULT_GPU_MEMORY_UTILIZATION)  # MiB 粒度下与 ceil(bytes) 一致
    ok = asr_gpu_preflight(ARGV, run=smi(one_gpu(required)), which=WHICH)
    assert ok.ok and ok.required_mib == required and ok.free_mib == required
    short = asr_gpu_preflight(ARGV, run=smi(one_gpu(required - 1)), which=WHICH)
    assert short.kind == "insufficient" and f"需要 {required} MiB" in short.reason and f"空闲 {required - 1} MiB" in short.reason


def test_live_like_reading():
    result = asr_gpu_preflight(ARGV, run=smi(one_gpu(1519)), which=WHICH)
    assert result.kind == "insufficient" and result.total_mib == 16303


def test_missing_nvidia_smi_is_unknown_not_enough():
    calls: list = []
    result = asr_gpu_preflight(ARGV, run=smi("", calls=calls), which=lambda n: None)
    assert result.kind == "unknown" and "未找到 nvidia-smi" in result.reason and calls == []


@pytest.mark.parametrize(
    ("out", "rc", "needle"),
    [
        ("", 9, "退出码 9"),
        ("0, GPU-x, [N/A], [N/A], Some GPU\n", 0, "无法解析"),
        ("garbage\n", 0, "无法解析"),
        ("\n", 0, "没有列出任何 GPU"),
    ],
)
def test_unparseable_or_failing_smi_is_unknown(out, rc, needle):
    result = asr_gpu_preflight(ARGV, run=smi(out, rc), which=WHICH)
    assert result.kind == "unknown" and needle in result.reason


def test_smi_timeout_is_unknown():
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 10)

    assert asr_gpu_preflight(ARGV, run=run, which=WHICH).kind == "unknown"


def test_multiple_gpus_without_clear_choice_is_unknown():
    out = one_gpu(16000) + "1, GPU-abcd, 24564, 24000, NVIDIA RTX A5000\n"
    result = asr_gpu_preflight(ARGV, run=smi(out), which=WHICH)
    assert result.kind == "unknown" and "2 张 GPU" in result.reason


def test_for_unit_reads_real_exec_start_and_unknown_on_read_failure():
    show = (
        "LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\nConditionResult=yes\n"
        "ConditionTimestampMonotonic=1\nExecMainCode=0\nExecMainStatus=0\nNRestarts=0\n"
        f"ExecStart={{ path=/v/bin/python ; argv[]={' '.join(ARGV)} --gpu-memory-utilization 0.5 ; pid=0 ; code=(null) ; status=0/0 }}\n"
    )

    def run(cmd, **kw):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, show, "")
        return subprocess.CompletedProcess(cmd, 0, one_gpu(9000), "")

    result = asr_gpu_preflight_for_unit("ollama-voice-asr.service", run=run, which=WHICH)
    assert result.ok and result.utilization == 0.5 and result.required_mib == math.ceil(16303 * 0.5)

    def broken(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus")

    bad = asr_gpu_preflight_for_unit("ollama-voice-asr.service", run=broken, which=WHICH)
    assert bad.kind == "unknown" and "无法读取" in bad.reason


def test_cli_exit_codes(monkeypatch, capsys):
    import _v3.gpu_preflight as mod
    from _v3.gpu_preflight import PreflightResult

    for kind, code in (("ok", 0), ("insufficient", 1), ("unknown", 2)):
        monkeypatch.setattr(mod, "asr_gpu_preflight_for_unit", lambda unit, k=kind: PreflightResult(kind=k, reason=f"r-{k}"))
        assert main(["asr", "--unit", "u.service"]) == code
    err = capsys.readouterr().err
    assert "r-insufficient" in err and "r-unknown" in err


def test_no_torch_or_vllm_imported():
    assert not [m for m in sys.modules if m.split(".")[0] in ("torch", "vllm")]
