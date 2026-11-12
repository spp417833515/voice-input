"""evdev 内核级全局快捷键，适用于 X11 和 Wayland（观察器/回退）。"""
from __future__ import annotations

import threading
import time

from gi.repository import GLib

from _hotkey import parse_hotkey_evdev
from _linux.hotkey_actions import HotkeyActionMixin, HotkeyContext
from _linux.uinput_device import UINPUT_DEVICE_NAME


class EvdevHotkeyGrabber(HotkeyActionMixin):
    """evdev 内核级全局快捷键，适用于 X11 和 Wayland。

    直接读取 /dev/input/event* 设备，绕过显示服务器限制。
    需要用户属于 input 组: sudo usermod -aG input $USER
    """

    def __init__(self, ctx: HotkeyContext) -> None:
        from evdev import InputDevice, ecodes, list_devices

        self._init_hotkey_actions(ctx)
        self.actions_enabled = True  # portal 接管时作为活动观察器，不触发动作
        self._running = False
        self._thread = None
        self._pressed_keys: set[int] = set()

        # 解析快捷键为 evdev 格式
        self._voice_combo = parse_hotkey_evdev(self.ctx.bindings.voice)
        self._screen_combo = parse_hotkey_evdev(self.ctx.bindings.screenshot)
        self._repeat_combo = parse_hotkey_evdev(self.ctx.bindings.repeat)
        print(f"  evdev: 语音快捷键 {self.ctx.bindings.voice} → trigger={self._voice_combo['trigger']} mods={self._voice_combo['modifiers']}")
        print(f"  evdev: 截图快捷键 {self.ctx.bindings.screenshot} → trigger={self._screen_combo['trigger']} mods={self._screen_combo['modifiers']}")
        print(f"  evdev: 重复快捷键 {self.ctx.bindings.repeat} → trigger={self._repeat_combo['trigger']} mods={self._repeat_combo['modifiers']}")

        # 查找键盘设备（排除自身 uinput 虚拟键盘，避免反馈循环）
        self._devices: list = []
        for path in list_devices():
            try:
                dev = InputDevice(path)
                if dev.name == UINPUT_DEVICE_NAME:
                    dev.close()
                    continue
                if self._is_real_keyboard(dev.capabilities()):
                    self._devices.append(dev)
                    print(f"  evdev: 监控键盘 {dev.name} ({dev.path})")
                else:
                    dev.close()
            except Exception:
                pass

        if not self._devices:
            raise RuntimeError(
                "未找到键盘设备。请确保用户属于 input 组: "
                "sudo usermod -aG input $USER 然后重新登录"
            )

    def grab(self) -> None:
        pass  # evdev 不需要 XGrabKey 式的 grab

    def ungrab(self) -> None:
        pass

    @staticmethod
    def _is_real_keyboard(caps) -> bool:
        """判定一个 evdev 设备是否为真键盘 (排除游戏鼠标宏键、特殊功能键等).

        条件: 含 EV_KEY, 不含鼠标按键 BTN_LEFT, 至少有 5 个字母键 + 含 SPACE.
        """
        from evdev import ecodes
        if ecodes.EV_KEY not in caps:
            return False
        keys = caps[ecodes.EV_KEY]
        if ecodes.BTN_LEFT in keys:
            return False  # 鼠标 (游戏鼠标的宏键也声明 KEY_A..KEY_Z, 必须靠 BTN_LEFT 排除)
        if ecodes.KEY_SPACE not in keys:
            return False
        letter_codes = [getattr(ecodes, f"KEY_{c}") for c in "ABCDEFGHIJKLM"]
        return sum(1 for c in letter_codes if c in keys) >= 5

    def _rescan_devices(self) -> None:
        """重新扫描键盘设备（USB 热插拔恢复）。"""
        from evdev import InputDevice, ecodes, list_devices

        existing_paths = {d.path for d in self._devices}
        for path in list_devices():
            if path in existing_paths:
                continue
            try:
                dev = InputDevice(path)
                if dev.name == UINPUT_DEVICE_NAME:
                    dev.close()
                    continue
                if self._is_real_keyboard(dev.capabilities()):
                    self._devices.append(dev)
                    print(f"  evdev: 重新发现键盘 {dev.name} ({dev.path})", flush=True)
                else:
                    dev.close()
            except Exception:
                pass

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _monitor(self) -> None:
        import select

        from evdev import ecodes

        print("  evdev: 监控线程已启动", flush=True)
        print(f"  evdev: 监控 {len(self._devices)} 个设备, fds={[d.fd for d in self._devices]}", flush=True)

        rescan_interval = 30.0  # 秒, KVM 切换/USB 热插拔的周期重扫节奏
        last_rescan = time.time()
        while self._running:
            now = time.time()
            if now - last_rescan >= rescan_interval:
                self._rescan_devices()
                last_rescan = now

            fds = {}
            for dev in list(self._devices):
                try:
                    fds[dev.fd] = dev
                except Exception as e:
                    print(f"  evdev: [ERROR] 获取fd失败 {dev.name}: {e}", flush=True)

            if not fds:
                print("  evdev: [WARN] 无可用设备, 尝试重新扫描...", flush=True)
                self._rescan_devices()
                last_rescan = time.time()
                time.sleep(3)
                continue

            try:
                r, _, _ = select.select(list(fds.keys()), [], [], 1.0)
            except Exception as e:
                # select 抛异常通常是某个 fd 变坏（EBADF：设备被拔/权限丢失）。
                # 原来只 sleep+continue 会 10Hz 空转刷屏且热键永久失效——这里直接
                # 关闭并丢弃全部当前设备、清残留按键，再重扫重建，让热键自愈。
                print(f"  evdev: [ERROR] select 异常: {type(e).__name__}: {e}, 重建设备", flush=True)
                for d in list(self._devices):
                    try:
                        d.close()
                    except Exception:
                        pass
                self._devices.clear()
                self._pressed_keys.clear()
                self._rescan_devices()
                last_rescan = time.time()
                time.sleep(0.5)
                continue

            for fd in r:
                dev = fds.get(fd)
                if not dev:
                    continue
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        self._handle_key_event(event.code, event.value)
                except OSError as e:
                    print(f"  evdev: [ERROR] 设备断开 {dev.name}: {e}", flush=True)
                    try:
                        dev.close()  # 补 close，否则 fd 泄漏（KVM 频繁切换会 EMFILE 拖垮整进程）
                    except Exception:
                        pass
                    try:
                        self._devices.remove(dev)
                    except ValueError:
                        pass
                    # 被拔设备可能有未收到 release 的残留按键 → 清账防幻触发录音
                    self._pressed_keys.clear()
                except Exception as e:
                    print(f"  evdev: [ERROR] 事件读取异常: {type(e).__name__}: {e}", flush=True)

    def _handle_key_event(self, code: int, value: int) -> None:
        """处理按键事件。value: 0=释放, 1=按下, 2=自动重复"""
        if value == 1:  # 按下
            self._pressed_keys.add(code)
            self._close_overlay_on_key()
            self._check_press(code)
        elif value == 0:  # 释放
            self._pressed_keys.discard(code)
            # 只按静态的 trigger keycode 判定，不在监控线程读 voice_active：
            # 快速点按时 press 的 voice_active=True 还排在主线程 idle 队列里未执行，
            # 若此处用 voice_active 门控会丢掉这次 release → 录音永不停止。
            # 统一交主线程 _do_release（在 press 的 idle 之后执行）按真实状态决定。
            if code == self._voice_combo["trigger"] and self.actions_enabled:
                print("  evdev: 语音触发键释放, 调度停止", flush=True)
                GLib.idle_add(self._schedule_release)
        # value == 2 (自动重复) → 忽略

    def _match_combo(self, combo: dict, pressed_kc: int | None = None) -> bool:
        """只有刚才按下的键是 trigger 才可能命中，避免陈 trigger 配后续任意键。"""
        if pressed_kc is not None and pressed_kc != combo["trigger"]:
            return False
        if combo["trigger"] not in self._pressed_keys:
            return False
        for mod_keys in combo["modifiers"]:
            if not (mod_keys & self._pressed_keys):
                return False
        return True

    def _check_press(self, pressed_kc: int) -> None:
        if not self.actions_enabled:
            return
        if self._match_combo(self._voice_combo, pressed_kc):
            print("  evdev: ★ 语音快捷键匹配！调度录音", flush=True)
            GLib.idle_add(self._on_voice_press)
        elif self._match_combo(self._screen_combo, pressed_kc):
            print("  evdev: ★ 截图快捷键匹配！调度截图", flush=True)
            GLib.idle_add(self._on_screenshot)
        elif self._match_combo(self._repeat_combo, pressed_kc):
            print("  evdev: ★ 重复快捷键匹配！调度重复", flush=True)
            GLib.idle_add(self._on_repeat)

    def cleanup(self) -> None:
        self._running = False
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
        if self._thread:
            self._thread.join(timeout=2)
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass
