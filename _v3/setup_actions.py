"""设置页的真实动作（生产入口注入；测试只用假传输/假进程，不触网、不调 live systemctl）。

- ollama_pull：Ollama 官方 POST /api/pull 流式进度（https://docs.ollama.com/api/pull）。
- ollama_install：官方 Linux 发行包 ollama-linux-amd64.tar.zst + 官方 sha256sum.txt 校验，解压到用户目录，
  不用 sudo、不写系统目录（https://docs.ollama.com/linux 手动安装流程的用户级变体）。
- asr_download：在隔离 ASR 环境里用官方 huggingface_hub snapshot_download 下载 Qwen3-ASR 权重。
- *_start：systemctl --user start 对应用户单元（不重试），读真实单元状态判定，仅运行/启动中才复查就绪。
每个动作失败都抛 LogicError，成功前必须复查真实状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from _activation import EXIT_GATED
from _contracts import ErrorCode, LogicError, SetupProgress
from _v3.setup_probe import probe_asr_weights
from _v3.setup_tasks import ProgressFn, SetupAction

Runner = Callable[..., subprocess.CompletedProcess]
OLLAMA_RELEASE_BASE = "https://github.com/ollama/ollama/releases/download"
OLLAMA_LINUX_ASSET = "ollama-linux-amd64.tar.zst"


def _percent(done: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(min(100.0, done * 100.0 / total), 1)


def ollama_pull_action(*, client_factory: Callable[[], httpx.Client], model: str) -> SetupAction:
    def run(report: ProgressFn) -> str:
        layers: dict[str, tuple[int, int]] = {}
        try:
            with client_factory() as client:
                with client.stream("POST", "/api/pull", json={"model": model, "stream": True}) as resp:
                    if resp.status_code >= 400:
                        body = resp.read().decode("utf-8", "replace")
                        raise LogicError(ErrorCode.dependency_failed, f"Ollama 拉取失败 HTTP {resp.status_code}: {body[:300]}")
                    last_status = ""
                    for line in resp.iter_lines():
                        if not line.strip():
                            continue
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise LogicError(ErrorCode.dependency_failed, f"Ollama 拉取进度无法解析: {line[:200]}") from exc
                        if item.get("error"):
                            raise LogicError(ErrorCode.dependency_failed, f"Ollama 拉取失败: {item['error']}")
                        last_status = str(item.get("status") or last_status)
                        digest = item.get("digest")
                        if digest and item.get("total"):
                            layers[digest] = (int(item.get("completed") or 0), int(item["total"]))
                        done = sum(c for c, _ in layers.values())
                        total = sum(t for _, t in layers.values())
                        report(SetupProgress(completed_bytes=done or None, total_bytes=total or None,
                                             percent=_percent(done, total), stage=last_status))
                if last_status != "success":
                    raise LogicError(ErrorCode.dependency_failed, f"Ollama 拉取未完成，最后状态: {last_status or '无'}")
                tags = client.get("/api/tags")
                tags.raise_for_status()
                names = {m.get("name") for m in tags.json().get("models") or []} | {
                    m.get("model") for m in tags.json().get("models") or []
                }
        except httpx.HTTPError as exc:
            raise LogicError(ErrorCode.dependency_failed, f"无法连接 Ollama: {exc}") from exc
        if model not in names:
            raise LogicError(ErrorCode.dependency_failed, f"拉取结束但 /api/tags 中没有 {model}")
        return f"{model} 已下载"

    return run


@dataclass(frozen=True)
class ExecCommand:
    """`systemctl show` 中 ExecCondition/ExecStart 的一条命令：argv 与最近一次退出状态（未运行过为 None）。"""

    argv: tuple[str, ...]
    status: int | None


@dataclass(frozen=True)
class UnitState:
    """`systemctl --user show` 读到的真实单元状态（只读）。"""

    unit: str
    load_state: str
    active_state: str
    sub_state: str
    result: str
    condition_result: str
    condition_timestamp_mono: int
    exec_main_code: int
    exec_main_status: int
    n_restarts: int
    exec_condition: tuple[ExecCommand, ...]
    exec_start: tuple[ExecCommand, ...]
    drop_in_paths: tuple[str, ...] = ()  # systemd DropInPaths（空格分隔的绝对路径；路径含空格时 systemd 本身也不加引号）

    def summary(self) -> str:
        return (
            f"ActiveState={self.active_state} SubState={self.sub_state} Result={self.result} "
            f"ConditionResult={self.condition_result} ExecMainCode={self.exec_main_code} "
            f"ExecMainStatus={self.exec_main_status} NRestarts={self.n_restarts}"
        )


_STATE_PROPS = (
    "LoadState", "ActiveState", "SubState", "Result", "ConditionResult", "ConditionTimestampMonotonic",
    "ExecMainCode", "ExecMainStatus", "NRestarts", "ExecCondition", "ExecStart", "DropInPaths",
)


def _parse_exec_commands(values: list[str]) -> tuple[ExecCommand, ...]:
    """解析 `{ path=… ; argv[]=a b c ; … ; status=78 }`（可多条）。argv 按空白切分（systemd 输出不加引号）。"""
    commands: list[ExecCommand] = []
    for value in values:
        for block in re.findall(r"\{(.*?)\}", value):
            fields = dict(
                (k.strip(), v.strip()) for k, _, v in (part.partition("=") for part in block.split(" ; ")) if k.strip()
            )
            if "argv[]" not in fields:
                continue
            status = re.match(r"-?\d+", fields.get("status", ""))
            commands.append(ExecCommand(argv=tuple(fields["argv[]"].split()), status=int(status.group()) if status else None))
    return tuple(commands)


def systemd_unit_state(unit: str, *, run: Runner = subprocess.run) -> UnitState:
    """只读 `systemctl --user show`。调用失败/非 0/缺字段抛 LogicError(dependency_failed)，不猜默认值。"""
    cmd = ["systemctl", "--user", "show", unit, *(f"--property={name}" for name in _STATE_PROPS)]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LogicError(ErrorCode.dependency_failed, f"无法读取 {unit} 状态：{exc}") from exc
    if proc.returncode != 0:
        raise LogicError(
            ErrorCode.dependency_failed,
            f"无法读取 {unit} 状态：systemctl show 退出码 {proc.returncode} {(proc.stderr or '').strip()[:200]}",
        )
    props: dict[str, list[str]] = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props.setdefault(key, []).append(value)
    scalar = ("LoadState", "ActiveState", "SubState", "Result", "ConditionResult")
    ints = ("ConditionTimestampMonotonic", "ExecMainCode", "ExecMainStatus", "NRestarts")
    missing = [name for name in (*scalar, *ints) if name not in props]
    if missing:
        raise LogicError(ErrorCode.dependency_failed, f"无法读取 {unit} 状态：systemctl show 缺少 {', '.join(missing)}")
    try:
        numbers = {name: int(props[name][0] or 0) for name in ints}
    except ValueError as exc:
        raise LogicError(ErrorCode.dependency_failed, f"无法读取 {unit} 状态：数值字段无法解析 {exc}") from exc
    return UnitState(
        unit=unit,
        load_state=props["LoadState"][0],
        active_state=props["ActiveState"][0],
        sub_state=props["SubState"][0],
        result=props["Result"][0],
        condition_result=props["ConditionResult"][0],
        condition_timestamp_mono=numbers["ConditionTimestampMonotonic"],
        exec_main_code=numbers["ExecMainCode"],
        exec_main_status=numbers["ExecMainStatus"],
        n_restarts=numbers["NRestarts"],
        exec_condition=_parse_exec_commands(props.get("ExecCondition", [])),
        exec_start=_parse_exec_commands(props.get("ExecStart", [])),
        drop_in_paths=tuple(" ".join(props.get("DropInPaths", [])).split()),
    )


def _is_project_gate_rejection(state: UnitState) -> bool:
    """本项目门闩拒绝的可验证证据：某条 ExecCondition 是 `… _activation.py check` 且最近退出码 = EXIT_GATED(78)。"""
    return any(
        len(cmd.argv) >= 2
        and Path(cmd.argv[-2]).name == "_activation.py"
        and cmd.argv[-1] == "check"
        and cmd.status == EXIT_GATED
        for cmd in state.exec_condition
    )


def failed_path_conditions(unit: str, *, run: Runner = subprocess.run, exists: Callable[[str], bool] = os.path.exists) -> list[str]:
    """读单元实际 ConditionPathExists（`systemctl --user cat`，含 drop-in 与空赋值重置），返回当前不满足的条目。
    读取失败抛 LogicError(dependency_failed)。含 % 说明符的条目无法在此求值，原样作为「无法判断」返回。"""
    try:
        proc = run(["systemctl", "--user", "cat", unit], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LogicError(ErrorCode.dependency_failed, f"无法读取 {unit} 单元文件：{exc}") from exc
    if proc.returncode != 0:
        raise LogicError(ErrorCode.dependency_failed, f"无法读取 {unit} 单元文件：退出码 {proc.returncode}")
    entries: list[str] = []
    section = ""
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif section == "[Unit]" and line.startswith("ConditionPathExists="):
            value = line.partition("=")[2].strip()
            entries = [] if not value else [*entries, value]
    failed: list[str] = []
    for value in entries:
        path = value.lstrip("|")
        negate = path.startswith("!")
        path = path.lstrip("!")
        if "%" in path:
            failed.append(f"{value}（含说明符，无法判断）")
        elif exists(path) == negate:
            failed.append(f"{'存在但要求不存在' if negate else '缺失'}：{path}")
    return failed


_RUNNING = ("active", "activating", "reloading")


def _terminal_failure(unit: str, state: UnitState, before: UnitState) -> str | None:
    """轮询中判定终态失败：失败/已退出/进入自动重启/重启计数增加。仍在运行或启动中返回 None。"""
    if state.active_state == "failed" or state.sub_state == "auto-restart" or state.n_restarts > before.n_restarts:
        return f"{unit} 启动后失败（{state.summary()}）；请查看 journalctl --user -u {unit}"
    if state.active_state not in _RUNNING:
        return f"{unit} 启动后已不在运行（{state.summary()}）；请查看 journalctl --user -u {unit}"
    return None


def classify_start(
    unit: str,
    before: UnitState,
    after: UnitState,
    *,
    path_failures: Callable[[], list[str]],
) -> tuple[str, str]:
    """start 之后立即判定：返回 (verdict, 说明)。verdict ∈ wait | gated | exec_condition | condition | failed | not_loaded | not_running。
    只有 wait 允许进入就绪轮询；其余都应立即失败。"""
    if after.load_state != "loaded":
        return "not_loaded", f"{unit} 未加载（LoadState={after.load_state}），请先安装/同步用户单元"
    if after.active_state in _RUNNING:
        failure = _terminal_failure(unit, after, before)
        return ("failed", failure) if failure else ("wait", "")
    fresh = after.condition_timestamp_mono != before.condition_timestamp_mono
    if fresh and after.result == "exec-condition":
        if _is_project_gate_rejection(after):
            return "gated", (
                f"本次运行未授权启动 {unit}：项目门闩拒绝（ExecCondition _activation.py check 退出码 {EXIT_GATED}），"
                "当前不在验收运行中。需以验收运行启动：在项目目录执行 ./start.sh --acceptance"
            )
        codes = ", ".join(f"{' '.join(c.argv)[:120]} → {c.status}" for c in after.exec_condition) or "无记录"
        return "exec_condition", f"{unit} 的 ExecCondition 拒绝启动，但无法确认是本项目门闩（{codes}）；请查看 journalctl --user -u {unit}"
    if fresh and after.condition_result == "no":
        try:
            failed = path_failures()
        except LogicError as exc:
            return "condition", f"{unit} 启动条件不满足（ConditionResult=no），且无法读取条件定义：{exc.message}"
        if failed:
            return "condition", f"{unit} 启动条件不满足：{'；'.join(failed)}。请先在设置页下载/安装所需文件"
        return "condition", f"{unit} 启动条件不满足（ConditionResult=no），未定位到缺失的 ConditionPathExists 文件；请查看 journalctl --user -u {unit}"
    if after.result not in ("success", "exec-condition"):
        return "failed", f"{unit} 启动失败（{after.summary()}）；请查看 journalctl --user -u {unit}"
    return "not_running", f"{unit} 在 systemctl start 后未运行（{after.summary()}）；请查看 journalctl --user -u {unit}"


def systemd_user_start_action(
    *,
    unit: str,
    ready: Callable[[], bool],
    run: Runner = subprocess.run,
    timeout_s: float = 180.0,
    interval_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    exists: Callable[[str], bool] = os.path.exists,
) -> SetupAction:
    """start 一次（不重试）→ 读真实单元状态按 classify_start 判定 → 只有运行/启动中才轮询 ready；
    轮询中每轮复读状态，失败/退出/自动重启立即结束；读状态失败立即结束；超时语义不变。"""

    def start(report: ProgressFn) -> str:
        report(SetupProgress(stage=f"启动 {unit}"))
        before = systemd_unit_state(unit, run=run)
        try:
            proc = run(["systemctl", "--user", "start", unit], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LogicError(ErrorCode.dependency_failed, f"无法调用 systemctl --user: {exc}") from exc
        if proc.returncode != 0:
            raise LogicError(ErrorCode.dependency_failed, f"启动 {unit} 失败: {(proc.stderr or proc.stdout or '').strip()[:300]}")
        after = systemd_unit_state(unit, run=run)
        verdict, message = classify_start(
            unit, before, after, path_failures=lambda: failed_path_conditions(unit, run=run, exists=exists)
        )
        if verdict != "wait":
            raise LogicError(ErrorCode.dependency_failed, message, {"unit": unit, "verdict": verdict})
        deadline = clock() + timeout_s
        while True:
            if ready():
                return f"{unit} 已就绪"
            failure = _terminal_failure(unit, systemd_unit_state(unit, run=run), before)
            if failure:
                raise LogicError(ErrorCode.dependency_failed, failure, {"unit": unit, "verdict": "failed"})
            if clock() >= deadline:
                break
            report(SetupProgress(stage=f"等待 {unit} 就绪"))
            sleep(interval_s)
        raise LogicError(ErrorCode.timeout, f"{unit} 在 {int(timeout_s)}s 内未就绪；请查看 journalctl --user -u {unit}")

    return start


def _expected_sha256(sums_text: str, asset: str) -> str:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*").removeprefix("./") == asset:
            return parts[0].lower()
    raise LogicError(ErrorCode.dependency_failed, f"官方 sha256sum.txt 中没有 {asset}")


def ollama_install_action(
    *,
    version: str,
    install_dir: Path,
    client_factory: Callable[[], httpx.Client],
    run: Runner = subprocess.run,
) -> SetupAction:
    """下载官方发行包到用户目录。install_dir 下应得到 bin/ollama 与 lib/ollama。"""

    def install(report: ProgressFn) -> str:
        if (install_dir / "bin" / "ollama").is_file():
            raise LogicError(ErrorCode.conflict, f"{install_dir} 已有 Ollama，未重复安装")
        base = f"{OLLAMA_RELEASE_BASE}/v{version}"
        download = install_dir.with_name(f".{install_dir.name}.download")
        staging = install_dir.with_name(f".{install_dir.name}.staging")
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            with client_factory() as client:
                sums = client.get(f"{base}/sha256sum.txt")
                sums.raise_for_status()
                expected = _expected_sha256(sums.text, OLLAMA_LINUX_ASSET)
                digest = hashlib.sha256()
                with client.stream("GET", f"{base}/{OLLAMA_LINUX_ASSET}") as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length") or 0)
                    done = 0
                    with download.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
                            digest.update(chunk)
                            done += len(chunk)
                            report(SetupProgress(completed_bytes=done, total_bytes=total or None,
                                                 percent=_percent(done, total), stage="下载 Ollama 官方发行包"))
        except httpx.HTTPError as exc:
            download.unlink(missing_ok=True)
            raise LogicError(ErrorCode.dependency_failed, f"下载 Ollama 失败: {exc}") from exc
        try:
            if digest.hexdigest() != expected:
                raise LogicError(ErrorCode.dependency_failed, "Ollama 发行包 sha256 与官方清单不一致，已拒绝安装")
            report(SetupProgress(stage="解压到用户目录"))
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            proc = run(["tar", "--zstd", "-xf", str(download), "-C", str(staging)],
                       capture_output=True, text=True, timeout=600, check=False)
            if proc.returncode != 0:
                raise LogicError(ErrorCode.dependency_failed, f"解压失败: {(proc.stderr or '').strip()[:300]}")
            if not (staging / "bin" / "ollama").is_file():
                raise LogicError(ErrorCode.dependency_failed, "发行包内没有 bin/ollama")
            os.replace(staging, install_dir)
        finally:
            download.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
        return f"Ollama {version} 已安装到 {install_dir}"

    return install


def asr_download_action(
    *,
    python: Path,
    worker: Path,
    repo_id: str,
    model_dir: Path,
    log_path: Path,
    env: dict[str, str],
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    interval_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> SetupAction:
    def download(report: ProgressFn) -> str:
        if not python.is_file():
            raise LogicError(ErrorCode.dependency_failed, f"识别运行环境未安装: {python}")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        total = {"bytes": 0}
        with log_path.open("w", encoding="utf-8") as log:
            proc = popen([str(python), str(worker), repo_id, str(model_dir)],
                         stdout=subprocess.PIPE, stderr=log, text=True, env=env)

            def read_stdout() -> None:
                for line in proc.stdout:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "total_bytes" in item:
                        total["bytes"] = int(item["total_bytes"])

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            while proc.poll() is None:
                done = probe_asr_weights(model_dir).bytes_on_disk if model_dir.exists() else 0
                report(SetupProgress(completed_bytes=done, total_bytes=total["bytes"] or None,
                                     percent=_percent(done, total["bytes"]), stage="下载识别模型权重"))
                sleep(interval_s)
            reader.join(timeout=5)
        if proc.returncode != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
            raise LogicError(ErrorCode.dependency_failed, f"识别模型下载失败（退出码 {proc.returncode}）: {tail}")
        probe = probe_asr_weights(model_dir)
        if not probe.complete:
            raise LogicError(ErrorCode.dependency_failed, "下载进程结束但权重不完整: " + "、".join(probe.missing))
        return f"{repo_id} 已下载"

    return download
