"""新启动 ASR sidecar 前的显存预检（逻辑层，只读 nvidia-smi 与单元 ExecStart，不导入 torch/vllm）。

与 vLLM 0.14.0 同源（venv-asr 内只读核对）：
- vllm/v1/worker/gpu_worker.py:233-234 `init_snapshot = MemorySnapshot(device)`；
  `requested_memory = request_memory(init_snapshot, cache_config)`；
- vllm/v1/worker/utils.py:253-273 `request_memory`：
  `requested = ceil(total_memory * gpu_memory_utilization)`，`free_memory < requested` 即 ValueError 拒启；
- vllm/utils/mem_utils.py MemorySnapshot.measure：`free_memory, total_memory = torch.cuda.mem_get_info(device)`。
gpu_memory_utilization 的实际来源：单元 ExecStart `… -m runtimes.asr serve …` 的参数，经 sidecar 自己的
`runtimes.asr.__main__.build_parser()` 解析（未显式传 `--gpu-memory-utilization` 时即
`runtimes.asr.protocol.DEFAULT_GPU_MEMORY_UTILIZATION`），再经 engine.load 传给 vLLM。本模块复用同一解析器，不另写阈值。

已知差异（如实）：nvidia-smi 的 MiB 读数与 cudaMemGetInfo 字节读数不是同一调用；vLLM 测量时自身 CUDA 上下文已占用
一部分显存，本预检测不到。所以本预检是「必要非充分」：通过后 vLLM 仍可能以同一条件拒启（会如实失败），
不通过则一定不应启动。探测失败/多卡无法确定一律不通过（失败≠够用）。「unit 已 active 不预检」属业务层。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import math
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from _contracts import LogicError
from _v3.setup_actions import Runner, systemd_unit_state

MIB = 1024 * 1024
ASR_MODULE = "runtimes.asr"


@dataclass(frozen=True)
class GpuMemory:
    index: str
    uuid: str
    name: str
    total_mib: int
    free_mib: int


@dataclass(frozen=True)
class PreflightResult:
    """kind：ok（够）/ insufficient（实测不够）/ unknown（无法判断，同样拒启）。"""

    kind: str
    reason: str
    utilization: float | None = None
    required_mib: int | None = None
    free_mib: int | None = None
    total_mib: int | None = None
    gpu: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def asr_utilization_from_argv(exec_argv: Sequence[str]) -> float:
    """从单元 ExecStart argv 取实际 gpu_memory_utilization：用 sidecar 自己的 build_parser 解析
    `-m runtimes.asr` 之后的参数。不是 sidecar 命令或参数无法解析抛 ValueError。"""
    from runtimes.asr.__main__ import build_parser

    argv = list(exec_argv)
    try:
        start = next(i for i in range(len(argv) - 1) if argv[i] == "-m" and argv[i + 1] == ASR_MODULE) + 2
    except StopIteration:
        raise ValueError(f"ExecStart 不是 `-m {ASR_MODULE}` sidecar 命令：{' '.join(argv)[:200]}") from None
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            args = build_parser().parse_args(argv[start:])
    except SystemExit:
        raise ValueError(f"sidecar 参数无法解析：{err.getvalue().strip()[-200:]}") from None
    util = float(args.gpu_memory_utilization)
    if not 0.0 < util <= 1.0:
        raise ValueError(f"gpu_memory_utilization={util} 不在 (0, 1]")
    return util


def query_gpus(*, run: Runner = subprocess.run, which: Callable[[str], str | None] = shutil.which) -> list[GpuMemory]:
    """nvidia-smi 实时读数。不存在/超时/非 0/无法解析抛 ValueError（原因写明）。"""
    if not which("nvidia-smi"):
        raise ValueError("未找到 nvidia-smi，无法读取显存")
    cmd = ["nvidia-smi", "--query-gpu=index,uuid,memory.total,memory.free,name", "--format=csv,noheader,nounits"]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"nvidia-smi 调用失败：{type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(f"nvidia-smi 退出码 {proc.returncode}：{(proc.stderr or proc.stdout or '').strip()[:200]}")
    gpus: list[GpuMemory] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",", 4)]
        try:
            if len(parts) != 5:
                raise ValueError
            gpus.append(GpuMemory(index=parts[0], uuid=parts[1], total_mib=int(parts[2]), free_mib=int(parts[3]), name=parts[4]))
        except ValueError:
            raise ValueError(f"nvidia-smi 输出无法解析：{line.strip()[:200]!r}") from None
    if not gpus:
        raise ValueError("nvidia-smi 没有列出任何 GPU")
    return gpus


def asr_gpu_preflight(
    exec_argv: Sequence[str],
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> PreflightResult:
    """按 sidecar 实际参数与实时显存判定能否新启动 ASR。只在单卡时判定；多卡无法确定 vLLM 用哪张 → unknown。"""
    try:
        util = asr_utilization_from_argv(exec_argv)
    except ValueError as exc:
        return PreflightResult(kind="unknown", reason=f"无法确定识别服务的显存占用比例：{exc}")
    try:
        gpus = query_gpus(run=run, which=which)
    except ValueError as exc:
        return PreflightResult(kind="unknown", reason=f"显存探测失败：{exc}", utilization=util)
    if len(gpus) != 1:
        listing = "；".join(f"#{g.index} {g.name} 空闲 {g.free_mib}/{g.total_mib} MiB" for g in gpus)
        return PreflightResult(
            kind="unknown",
            reason=f"检测到 {len(gpus)} 张 GPU（{listing}），无法确定 vLLM 将使用哪一张，不做假定",
            utilization=util,
        )
    gpu = gpus[0]
    required_bytes = math.ceil(gpu.total_mib * MIB * util)  # 同 vllm request_memory：ceil(total × util)
    required_mib = math.ceil(required_bytes / MIB)
    fields = dict(utilization=util, required_mib=required_mib, free_mib=gpu.free_mib, total_mib=gpu.total_mib, gpu=f"#{gpu.index} {gpu.name}")
    if gpu.free_mib * MIB < required_bytes:
        return PreflightResult(
            kind="insufficient",
            reason=(
                f"显存不足：识别服务需要 {required_mib} MiB（{util} × 总量 {gpu.total_mib} MiB），"
                f"当前空闲 {gpu.free_mib} MiB（{gpu.name}）。请先关闭占用显存的程序"
            ),
            **fields,
        )
    return PreflightResult(
        kind="ok",
        reason=f"显存预检通过：需要 {required_mib} MiB（{util} × {gpu.total_mib} MiB），空闲 {gpu.free_mib} MiB（{gpu.name}）",
        **fields,
    )


def asr_gpu_preflight_for_unit(
    unit: str,
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> PreflightResult:
    """读单元真实 ExecStart（systemd_unit_state，只读）后预检；读不到 → unknown。"""
    try:
        state = systemd_unit_state(unit, run=run)
    except LogicError as exc:
        return PreflightResult(kind="unknown", reason=exc.message)
    if len(state.exec_start) != 1:
        return PreflightResult(kind="unknown", reason=f"{unit} 的 ExecStart 有 {len(state.exec_start)} 条，无法确定 sidecar 参数")
    return asr_gpu_preflight(state.exec_start[0].argv, run=run, which=which)


def main(argv: Sequence[str] | None = None) -> int:
    """`python3 -m _v3.gpu_preflight asr --unit <asr 单元>`：0=够；1=实测不够；2=无法判断（同样拒启）。原因写 stderr。"""
    parser = argparse.ArgumentParser(prog="python3 -m _v3.gpu_preflight", description="ASR 新启动前显存预检（只读）")
    parser.add_argument("target", choices=("asr",))
    parser.add_argument("--unit", required=True, help="ASR 用户单元名（调用方传入其唯一来源）")
    args = parser.parse_args(argv)
    result = asr_gpu_preflight_for_unit(args.unit)
    if result.ok:
        print(result.reason)
        return 0
    print(result.reason, file=sys.stderr)
    return 1 if result.kind == "insufficient" else 2


if __name__ == "__main__":
    raise SystemExit(main())
