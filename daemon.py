"""桌面守护进程入口（薄）：只做装配与 GTK 主循环。

所有平台语义在 `_linux/*`（热键抓取/观察、portal、选区截图、粘贴与剪贴板回收、修饰键清理、
watchdog、托盘、WebKit 窗口），业务流程在 `_desktop/*`（录音会话上传、jobs 认领投递确认、
重复键、截图译文），产品状态只经 v1 API 事件流显示。本文件不含业务规则。
"""
from __future__ import annotations

import os

# 必须在 import gi 之前：Wayland 且有 XWayland 时 GTK 走 x11（XGrabKey/XTest/状态栏定位都依赖它）。
_SESSION_TYPE = os.environ.get("XDG_SESSION_TYPE", "x11")
if _SESSION_TYPE == "wayland" and os.environ.get("DISPLAY"):
    os.environ["GDK_BACKEND"] = "x11"

from _activation import exit_if_blocked  # noqa: E402  标准库-only，先于 gi/设备

import atexit  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from _config import ConfigStore  # noqa: E402
from _desktop.actions import DesktopActions  # noqa: E402
from _desktop.api import DesktopApi  # noqa: E402
from _desktop.delivery import DeliveryLoop, LastDelivered, RepeatAction  # noqa: E402
from _desktop.screenshot import ScreenshotFlow  # noqa: E402
from _desktop.status import PlatformStatus  # noqa: E402
from _desktop.voice import VoiceFlow, new_job_id  # noqa: E402
from _focus import X11FocusProbe  # noqa: E402
from _hotkey import HotkeyBindings, format_hotkey  # noqa: E402
from _inject import FocusBoundInjector  # noqa: E402
from _linux.clipboard import SystemClipboard  # noqa: E402
from _linux.conflicts import scan_hotkey_conflicts  # noqa: E402
from _linux.glib_scheduler import GLibScheduler  # noqa: E402
from _linux.hotkey_actions import HotkeyContext  # noqa: E402
from _linux.hotkey_manager import HotkeyManager  # noqa: E402
from _linux.ibus import IBusCommitter  # noqa: E402
from _linux.inject_guard import InjectGuard  # noqa: E402
from _linux.mic import SoundDeviceSource, play_beep  # noqa: E402
from _linux.modifiers import ModifierJanitor  # noqa: E402
from _linux.overlay_windows import StatusWindow, TranslateWindow  # noqa: E402
from _linux.paste_backend import LinuxPasteBackend  # noqa: E402
from _linux.portal_dbus import ensure_dbus_glib_loop, portal_interface_version  # noqa: E402
from _linux.screenshot import ScreenCapture, detect_screenshot_tool  # noqa: E402
from _linux.sd_notify import sd_notify  # noqa: E402
from _linux.singleton import acquire_singleton_lock  # noqa: E402
from _linux.startup_check import startup_issues  # noqa: E402
from _linux.tray import create_tray_icon  # noqa: E402
from _linux.uinput_device import open_uinput  # noqa: E402
from _linux.watchdog import IdleSafetyWatchdog  # noqa: E402

VERSION = "3.0.0"
PROJECT_ROOT = Path(__file__).resolve().parent
API_ORIGIN = "https://127.0.0.1:17945"
CERT = PROJECT_ROOT / "certs" / "cert.pem"
CONFIG = PROJECT_ROOT / ".config.json"  # 与 _v3.production.ProductionPaths.config 同一文件，只读
WATCHDOG_MS = 2000


def load_bindings() -> HotkeyBindings:
    cfg = ConfigStore(CONFIG).load()
    return HotkeyBindings(voice=cfg.hotkey_voice, screenshot=cfg.hotkey_screenshot, repeat=cfg.hotkey_repeat)


def main() -> None:
    exit_if_blocked()  # 门闩未关且非验收运行：78 退出，不抓键、不开设备（单元 RestartPreventExitStatus=78）
    lock_fd = acquire_singleton_lock()  # fd 必须保持引用防止 GC 关闭（finally 里关闭）
    bindings = load_bindings()

    try:
        uinput = open_uinput()
    except Exception as exc:
        uinput = None
        print(f"  uinput: 不可用，使用 pynput 回退 ({exc})")
        if _SESSION_TYPE == "wayland":
            print("  ⚠ 警告: Wayland 下 uinput 不可用将导致粘贴功能失效！")
            print("    请确保: sudo chmod 660 /dev/uinput && sudo chgrp input /dev/uinput")
    janitor = ModifierJanitor(session_type=_SESSION_TYPE, uinput=uinput, xdotool=shutil.which("xdotool"))
    # 启动即清残留：上一代进程崩溃时 XTest 层按下的修饰键会跨进程残留。
    janitor.clear_xtest()

    ensure_dbus_glib_loop()
    tool = detect_screenshot_tool()
    issues = startup_issues(
        bindings,
        portal_version=portal_interface_version,
        screenshot_tool=tool,
        conflicts=scan_hotkey_conflicts,
    )

    print(f"Ollama Voice Input v{VERSION} 守护进程已启动")
    print(f"  会话类型: {_SESSION_TYPE}")
    print(f"  GDK 后端: {os.environ.get('GDK_BACKEND', 'auto')}")
    print(f"  语音输入: {format_hotkey(bindings.voice)}  按住说话，松开自动输入")
    print(f"  截图翻译: {format_hotkey(bindings.screenshot)}   框选截图，OCR+本地翻译")
    print(f"  重复输入: {format_hotkey(bindings.repeat)}  重复上一次输入")
    print(f"  截图后端: {tool or '不可用'}")
    print(f"  API 地址: {API_ORIGIN}")
    if uinput is not None:
        print("  uinput: 已初始化（内核级按键模式）")
    if shutil.which("xdotool"):
        print("  xdotool: 已找到（修饰键安全粘贴模式）")
    else:
        print("  xdotool: 未安装，粘贴时可能影响快捷键（sudo apt install xdotool）")
    for issue in issues:
        print(f"  ⚠ {issue}")

    ibus = IBusCommitter.connect()
    print(f"  IBus: {'直接提交可用' if ibus.available else '剪贴板模式'}（{ibus.reason}）")

    api = DesktopApi(API_ORIGIN, cert=CERT)
    status = PlatformStatus(api.post_platform_event)
    probe = X11FocusProbe(session_type=_SESSION_TYPE)
    guard = InjectGuard()
    backend = LinuxPasteBackend(
        session_type=_SESSION_TYPE, guard=guard, janitor=janitor, clipboard=SystemClipboard(), ibus=ibus
    )
    last = LastDelivered()
    capture = ScreenCapture(tool=tool)

    def open_translate(png: bytes, rect, events):
        return TranslateWindow(origin=API_ORIGIN, cert=CERT, png=png, rect=rect, events=events)

    voice = VoiceFlow(
        api=api,
        status=status,
        probe=probe,
        source_factory=SoundDeviceSource,
        beep=play_beep,  # play_beep 自己移到后台线程，不阻塞 GTK 主循环
    )
    actions = DesktopActions(
        voice=voice,
        screenshot=ScreenshotFlow(
            capture=capture, open_overlay=open_translate, status=status, scheduler=GLibScheduler()
        ),
        repeat=RepeatAction(api=api, last=last, probe=probe, status=status, job_ids=new_job_id),
    )

    status_window = StatusWindow(origin=API_ORIGIN, cert=CERT)
    tray = create_tray_icon(settings_url=f"{API_ORIGIN}/setup", toggle_status=status_window.set_visible)

    manager = HotkeyManager(
        HotkeyContext(actions=actions, bindings=bindings, janitor=janitor, guard=guard, session_type=_SESSION_TYPE)
    )
    manager.start()

    delivery = DeliveryLoop(jobs=api.jobs, injector=FocusBoundInjector(probe, backend), last=last)
    threading.Thread(target=delivery.run_forever, name="jobs-delivery", daemon=True).start()

    # 退出信号统一走干净退出（SIGTERM=systemctl stop，SIGINT=Ctrl-C，SIGHUP=终端关闭）。
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda s, f: GLib.idle_add(Gtk.main_quit))
    atexit.register(janitor.panic_release_all)
    watchdog = IdleSafetyWatchdog(
        manager=manager, janitor=janitor, guard=guard, overlay_active=actions.overlay_active, notify=sd_notify
    )
    GLib.timeout_add(WATCHDOG_MS, watchdog.tick)
    sd_notify("READY=1")

    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        janitor.panic_release_all()  # 先释放残留修饰键 + 指针 grab，再拆设备
        delivery.stop()
        manager.cleanup()
        capture.cleanup()
        status.close()
        del tray
        os.close(lock_fd)
        if uinput is not None:
            try:
                uinput.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
