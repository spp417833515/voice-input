"""平台模块与薄入口可导入（只导入，不运行 main、不建窗口、不抓键）。

测试解释器有 GTK3 时进程内导入；隔离测试 venv 没有 gi 时，用生产单元同一解释器（项目 .venv，
systemd daemon 单元的 ExecStart）在子进程里只做 import，去掉 DISPLAY/WAYLAND_DISPLAY，不安装任何包。
两者都没有才跳过。
"""
from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_PY = ROOT / ".venv" / "bin" / "python3"

MODULES = (
    "_linux.inject_guard", "_linux.x11keys", "_linux.sd_notify", "_linux.uinput_device", "_linux.modifiers",
    "_linux.clipboard", "_linux.ibus", "_linux.paste_backend", "_linux.watchdog", "_linux.conflicts",
    "_linux.portal_dbus", "_linux.portal_screenshot", "_linux.geometry", "_linux.selection",
    "_linux.screenshot_types", "_linux.screenshot", "_linux.hotkey_actions", "_linux.hotkey_x11",
    "_linux.hotkey_xinput", "_linux.hotkey_evdev", "_linux.hotkey_portal", "_linux.hotkey_manager",
    "_linux.mic", "_linux.overlay_windows", "_linux.tray", "_linux.singleton", "_linux.startup_check",
    "_desktop.actions", "_desktop.api", "_desktop.delivery", "_desktop.screenshot", "_desktop.status",
    "_desktop.voice", "daemon", "app",
)

_CHILD = """
import importlib, sys
mods = sys.argv[1:]
for m in mods:
    importlib.import_module(m)
import daemon, app
assert callable(daemon.main) and callable(app.create_production_app)
assert not hasattr(app, "app")
print("imported", len(mods))
"""


def _gtk_in_process() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        return True
    except (ImportError, ValueError):
        return False


def test_platform_modules_import(monkeypatch):
    if _gtk_in_process():
        monkeypatch.delenv("DISPLAY", raising=False)
        for name in MODULES:
            importlib.import_module(name)
        assert callable(importlib.import_module("daemon").main)
        return
    if not PRODUCTION_PY.exists():
        pytest.skip("无 GTK3，且没有生产解释器 .venv/bin/python3")
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "NOTIFY_SOCKET")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [str(PRODUCTION_PY), "-c", _CHILD, *MODULES],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert f"imported {len(MODULES)}" in proc.stdout
