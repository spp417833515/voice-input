"""生产启用门闩的唯一来源（只用标准库：start.sh/restart.sh/install.sh 在建 venv 前、
systemd 单元 ExecCondition、daemon 与 Web 工厂启动时都调用同一判定）。

- 官方/真机门闩 PRODUCTION_UNMET_GATES 是源码常量，只在对应验收证据关闭后由代码变更减少；
  不因模型已下载、接线完成或任何文件/环境而自动清空。
- 门闩未关时唯一允许的运行方式是用户显式 `./start.sh --acceptance`：脚本给四个用户单元写
  drop-in `Environment=OVI_ACCEPTANCE_RUN=1`。它不清空门闩，状态/设置页照常显示全部未达项。
- 被拒时以 EXIT_GATED(78, EX_CONFIG) 退出：单元 ExecCondition 把它当“条件不满足→跳过”，
  daemon/server 用 RestartPreventExitStatus=78，不会崩溃循环、不会抓键。

CLI：python3 _activation.py check  → 0 允许（official / acceptance）；78 拒绝并把门闩打印到 stderr。
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping

# 已关闭：P2 官方 ASR 长音频刷新——按用户口径（通常 0.5–1s，词句交界偶发约 1.5s）由 r9 真实 GPU 链路证据
# 与独立复核于 2026-11-11 03:03 America/New_York 关闭（v3-asr-r9-default-recheck.md）；仍存限制见
# _contracts.ASR_STREAMING_BLOCKERS。下列门闩未关前所有入口照样拒启。
PRODUCTION_UNMET_GATES: tuple[str, ...] = (
    "官方 qwen3.5:4b 只纠错、本地翻译、与 ASR 同机并存未验收（P3/P4）。",
    "真机联调未验收：真实 TLS WebKit、麦克风、键鼠注入、剪贴板回收、局域网手机。",
)

ACCEPTANCE_ENV = "OVI_ACCEPTANCE_RUN"
EXIT_GATED = 78


def activation_mode(environ: Mapping[str, str]) -> str:
    """official：门闩已全部关闭；acceptance：门闩未关但用户显式验收运行；blocked：拒绝。"""
    if not PRODUCTION_UNMET_GATES:
        return "official"
    if environ.get(ACCEPTANCE_ENV) == "1":
        return "acceptance"
    return "blocked"


def gate_message() -> str:
    lines = ["生产门闩未关闭，拒绝启动（不 enable、不 start 任何服务）："]
    lines += [f"  - {item}" for item in PRODUCTION_UNMET_GATES]
    lines.append("如需真机验收：./start.sh --acceptance（状态/设置页仍显示全部未达门闩）")
    return "\n".join(lines)


def exit_if_blocked(environ: Mapping[str, str] | None = None) -> str:
    """进程入口用：被拒时打印门闩并以 EXIT_GATED 退出。"""
    mode = activation_mode(os.environ if environ is None else environ)
    if mode == "blocked":
        print(gate_message(), file=sys.stderr)
        raise SystemExit(EXIT_GATED)
    return mode


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["check"]:
        print("用法: python3 _activation.py check", file=sys.stderr)
        return 2
    mode = activation_mode(os.environ)
    if mode == "blocked":
        print(gate_message(), file=sys.stderr)
        return EXIT_GATED
    print(mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
