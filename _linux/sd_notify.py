"""向 systemd 发送状态通知（READY=1 / WATCHDOG=1）。"""
from __future__ import annotations

import os
import socket
from collections.abc import Mapping


def sd_notify(state: str, environ: Mapping[str, str] | None = None) -> bool:
    """纯 socket 实现，无第三方依赖。非 systemd 运行（NOTIFY_SOCKET 未设置）或任何
    异常时静默 no-op —— 手动前台运行完全不受影响。返回是否已发出。
    """
    env = os.environ if environ is None else environ
    addr = env.get("NOTIFY_SOCKET")
    if not addr:
        return False
    try:
        if addr[0] == "@":  # systemd 抽象命名空间地址
            addr = "\0" + addr[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode("utf-8"), addr)
        return True
    except Exception:
        return False
