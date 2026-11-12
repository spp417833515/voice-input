"""单例锁，确保只有一个守护进程运行。"""
from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path


def acquire_singleton_lock() -> int:
    """获取单例锁，确保只有一个守护进程运行。返回锁文件 fd（进程退出自动释放）。"""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    lock_path = Path(runtime_dir) / "ollama-voice-input.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # 读取已有进程的 PID
        try:
            existing_pid = os.read(fd, 32).decode().strip()
        except Exception:
            existing_pid = "unknown"
        os.close(fd)
        print(f"错误: 守护进程已在运行 (PID {existing_pid})，不能重复启动。")
        print("  如需重启，请先停止现有进程: systemctl --user restart ollama-voice-daemon")
        sys.exit(1)
    # 写入当前 PID
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode())
    return fd  # fd 必须保持打开，进程退出时内核自动释放 flock
