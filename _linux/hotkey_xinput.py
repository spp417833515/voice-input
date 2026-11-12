"""XInput2 RawKey 全局快捷键 (X server 客户端层 raw event stream)。

与 XGrabKey 的差异: XInput2 raw 监听设备层事件, 不依赖 X server grab 链,
能拦截 XTest / XSendEvent 注入按键 (KVM / Synergy / Logitech Flow 场景).
与 evdev 的差异: 走 X server, 不需要 /dev/input/event* 设备节点.
"""
from __future__ import annotations

import sys
import time

from gi.repository import GLib
from Xlib import X, XK
from Xlib import display as xdisplay
from Xlib.ext import xinput as xi_ext
from Xlib.protocol import rq as xrq

from _linux.hotkey_actions import HotkeyActionMixin, HotkeyContext
from _linux.uinput_device import UINPUT_DEVICE_NAME
from _linux.x11keys import parse_hotkey

# 智能卡键检测阈值（针对 Synergy/XTest 丢 key-up 的幽灵修饰键）：
# 只在"修饰键真按下 + (持续超 SOFT 秒且键盘空闲超 IDLE 秒)"或"持续超 HARD 秒"时
# 才判定卡住并清理 —— 绝不误伤用户正常的长按修饰键。
STUCK_MOD_HARD_SECONDS = 6.0   # 无条件判定：没人会正常按住修饰键这么久
STUCK_MOD_SOFT_SECONDS = 2.5
STUCK_MOD_IDLE_SECONDS = 1.5   # 键盘无新按键事件（空闲）
# 思路②：打字(按非修饰键)时，若某修饰键已持续按下超此秒数，即时清理(比看门狗更快)。
# 正常"按住 Ctrl 连敲"通常是秒内连击，很少按住 Ctrl 超 3s 才敲键，故误伤概率极低。
STUCK_MOD_KEYPRESS_SECONDS = 3.0
# 热键匹配用：修饰键位图显示按下但持续超过此秒数 → 视为幽灵卡键，不参与命中。
# 正常组合是"先按修饰键再按触发键"，间隔几乎总 <1s；幽灵 Ctrl 常挂数分钟。
# 宁可要求用户重按修饰键，也不要用卡死的 Ctrl 把 Alt+Z 误判成 Ctrl+Alt+Z。
STUCK_MOD_MATCH_SECONDS = 3.0


def xi_source_ids_by_name(dpy, name: str) -> set[int]:
    """查出 XInput2 中指定设备名的全部 deviceid（slave sourceid）。一行调用。"""
    ids: set[int] = set()
    try:
        reply = dpy.xinput_query_device(xi_ext.AllDevices)
    except Exception:
        return ids
    for info in getattr(reply, "devices", None) or ():
        if getattr(info, "name", None) != name:
            continue
        did = getattr(info, "deviceid", None)
        if did:
            ids.add(int(did))
    return ids


def _xi_source_ids_matching(dpy, needle: str) -> set[int]:
    """查出 XInput2 中设备名包含 needle 的全部 deviceid（XTEST 等）。"""
    ids: set[int] = set()
    try:
        reply = dpy.xinput_query_device(xi_ext.AllDevices)
    except Exception:
        return ids
    for info in getattr(reply, "devices", None) or ():
        name = getattr(info, "name", None) or ""
        if needle.lower() not in name.lower():
            continue
        did = getattr(info, "deviceid", None)
        if did:
            ids.add(int(did))
    return ids


# RawKey 事件的 wire format (xXIRawEvent in XI2proto.h):
# deviceid(u16) time(u32) detail(u32=keycode) sourceid(u16) valuators_len(u16) flags(u32) pad(u32)
_XI_RAW_EVENT_DATA = xrq.Struct(
    xrq.Card16('deviceid'),
    xrq.Card32('time'),
    xrq.Card32('detail'),
    xrq.Card16('sourceid'),
    xrq.Card16('valuators_len'),
    xrq.Card32('flags'),
    xrq.Pad(4),
)


class XInputHotkeyGrabber(HotkeyActionMixin):
    """XInput2 RawKey 全局快捷键管理, 通过 GLib.io_add_watch 集成到 GTK 事件循环.

    raw 事件没有 modifier mask, 自己用 keycode set 维护按下状态.
    与 HotkeyGrabber 接口契约一致: voice_active / start / cleanup / grab / ungrab.
    """

    def __init__(self, ctx: HotkeyContext) -> None:
        self._init_hotkey_actions(ctx)
        self.actions_enabled = True  # portal 接管时作为活动观察器，不触发动作
        self.only_xtest_sources = False  # portal 接管时只处理旧 XTEST 注入
        self._pressed: set[int] = set()
        self._ignore_source_ids: set[int] = set()
        self._xtest_source_ids: set[int] = set()

        self.dpy = xdisplay.Display()
        self.dpy.set_error_handler(self._ignore_error)
        if not self.dpy.has_extension('XInputExtension'):
            self.dpy.close()
            raise RuntimeError("X server 未启用 XInputExtension")

        self._major = self.dpy.display.get_extension_major(xi_ext.extname)
        qv = self.dpy.xinput_query_version()
        if qv.major_version < 2:
            self.dpy.close()
            raise RuntimeError(
                f"XInput 版本过低 ({qv.major_version}.{qv.minor_version}), 需要 >= 2.0"
            )

        # 注册 RawKey event-data parser (一次性, 注册后由 display 持有)
        try:
            self.dpy.ge_add_event_data(self._major, xi_ext.RawKeyPress, _XI_RAW_EVENT_DATA)
            self.dpy.ge_add_event_data(self._major, xi_ext.RawKeyRelease, _XI_RAW_EVENT_DATA)
        except Exception as exc:
            self.dpy.close()
            raise RuntimeError(f"无法注册 XInput2 RawKey parser: {exc}") from exc

        self.root = self.dpy.screen().root

        # 智能卡键检测：所有修饰键 keycode 集 + 各自按下起始时刻 + 最近按键活动时刻
        self._mod_kcs: set[int] = set()
        for _nm in ("Control_L", "Control_R", "Shift_L", "Shift_R",
                    "Alt_L", "Alt_R", "Super_L", "Super_R", "Meta_L", "Meta_R"):
            _ks = XK.string_to_keysym(_nm)
            if _ks:
                _kc = self.dpy.keysym_to_keycode(_ks)
                if _kc:
                    self._mod_kcs.add(_kc)
        self._mod_down_since: dict[int, float] = {}
        self._last_key_activity = time.monotonic()
        self._last_stuck_check = 0.0  # 打字时卡键检测的节流时间戳

        # 解析三个快捷键为 (trigger_keycode, [modifier_keycode_set, ...])
        self._voice_combo = self._compile_hotkey(self.ctx.bindings.voice)
        self._screen_combo = self._compile_hotkey(self.ctx.bindings.screenshot)
        self._repeat_combo = self._compile_hotkey(self.ctx.bindings.repeat)
        print(
            f"  XInput2: 语音 {self.ctx.bindings.voice} "
            f"→ trigger=kc{self._voice_combo['trigger']} mods={self._voice_combo['modifiers']}"
        )
        print(
            f"  XInput2: 截图 {self.ctx.bindings.screenshot} "
            f"→ trigger=kc{self._screen_combo['trigger']} mods={self._screen_combo['modifiers']}"
        )
        print(
            f"  XInput2: 重复 {self.ctx.bindings.repeat} "
            f"→ trigger=kc{self._repeat_combo['trigger']} mods={self._repeat_combo['modifiers']}"
        )

    def _compile_hotkey(self, hotkey_str: str) -> dict:
        """解析快捷键字符串 → {'trigger': keycode, 'modifiers': [set, ...]} (X11 keycode)."""
        mask, keysym = parse_hotkey(hotkey_str)
        trigger_kc = self.dpy.keysym_to_keycode(keysym)
        if not trigger_kc:
            raise ValueError(f"无法解析快捷键 {hotkey_str} 的触发键 keysym=0x{keysym:x}")
        modifiers = []
        if mask & X.ControlMask:
            modifiers.append(self._kc_set([XK.XK_Control_L, XK.XK_Control_R]))
        if mask & X.ShiftMask:
            modifiers.append(self._kc_set([XK.XK_Shift_L, XK.XK_Shift_R]))
        if mask & X.Mod1Mask:
            modifiers.append(
                self._kc_set([XK.XK_Alt_L, XK.XK_Alt_R, XK.XK_Meta_L, XK.XK_Meta_R])
            )
        if mask & X.Mod4Mask:
            modifiers.append(self._kc_set([XK.XK_Super_L, XK.XK_Super_R]))
        return {'trigger': trigger_kc, 'modifiers': modifiers}

    def _kc_set(self, keysyms: list[int]) -> set[int]:
        out: set[int] = set()
        for ks in keysyms:
            kc = self.dpy.keysym_to_keycode(ks)
            if kc:
                out.add(kc)
        return out

    @staticmethod
    def _ignore_error(err, *args):
        pass

    def grab(self) -> None:
        pass  # XInput2 raw 是被动监听, 不需要 grab

    def ungrab(self) -> None:
        pass

    def start(self) -> None:
        mask = xi_ext.RawKeyPressMask | xi_ext.RawKeyReleaseMask
        self.root.xinput_select_events([(xi_ext.AllMasterDevices, mask)])
        self.dpy.flush()
        self._refresh_ignore_sources()
        GLib.io_add_watch(self.dpy.fileno(), GLib.IO_IN, self._on_xlib_event)

    def _refresh_ignore_sources(self) -> None:
        """只滤自家 uinput。XTEST 单独记账，portal 接管时只认 XTEST 动作。"""
        self._ignore_source_ids = xi_source_ids_by_name(self.dpy, UINPUT_DEVICE_NAME)
        self._xtest_source_ids = _xi_source_ids_matching(self.dpy, "XTEST")

    def _is_injected_source(self, sourceid: int) -> bool:
        return bool(sourceid) and sourceid in self._ignore_source_ids

    def _is_xtest_source(self, sourceid: int) -> bool:
        return bool(sourceid) and sourceid in self._xtest_source_ids

    def _on_xlib_event(self, fd, condition) -> bool:
        try:
            self.dpy.sync()
        except Exception as e:
            print(f"XInput2 sync error: {e}", file=sys.stderr)
            return True
        while self.dpy.pending_events() > 0:
            try:
                e = self.dpy.next_event()
                if getattr(e, 'type', None) == X.MappingNotify:
                    self._refresh_ignore_sources()
                    continue
                if getattr(e, 'extension', None) != self._major:
                    continue
                evt = getattr(e, 'evtype', None)
                if evt not in (xi_ext.RawKeyPress, xi_ext.RawKeyRelease):
                    continue
                if self._is_injected_source(getattr(e.data, 'sourceid', 0)):
                    continue
                sourceid = int(getattr(e.data, "sourceid", 0))
                if evt == xi_ext.RawKeyPress:
                    self._handle_press(e.data.detail, sourceid)
                else:
                    self._handle_release(e.data.detail, sourceid)
            except Exception as e:
                print(f"XInput2 event error: {e}", file=sys.stderr)
        return True

    def _handle_press(self, keycode: int, sourceid: int = 0) -> None:
        # 找不到 uinput 设备时，时间窗仍吞 press，避免回流自触发。
        # 正常路径按 sourceid 过滤，不再吞用户真实按键。
        if not self._ignore_source_ids and self.ctx.guard.active():
            return
        self._last_key_activity = time.monotonic()  # 记录真实按键活动(供卡键检测判空闲)
        if keycode in self._mod_kcs:
            # 记录修饰键按下时刻(Synergy 的 down 可靠, 丢的是 up), 供卡键"持续时长"判定
            self._mod_down_since.setdefault(keycode, self._last_key_activity)
        elif (not self.voice_active
                and self._last_key_activity - self._last_stuck_check > 0.25):
            # 思路②事件驱动自愈：打字(按非修饰键)时若某修饰键已卡住(持续按下超阈值)，
            # 立即清 —— 比 2s 看门狗更快治"边打字边卡"；0.25s 节流避免自动重复刷 X。
            self._last_stuck_check = self._last_key_activity
            if self._has_lingering_modifier(STUCK_MOD_KEYPRESS_SECONDS):
                # 与 _match_combo 同路径：清 XTest + 剔除 raw 账本中的幽灵修饰键
                lingering = self._lingering_modifiers()
                ghosts = {
                    kc: h for kc, h in lingering.items()
                    if h > STUCK_MOD_KEYPRESS_SECONDS
                }
                print(
                    f"[stuck] 打字时检测到卡住的修饰键 {list(ghosts)}, 立即清理",
                    flush=True,
                )
                self.ctx.janitor.clear_xtest()
                for kc in ghosts:
                    self._pressed.discard(kc)
                    self._mod_down_since.pop(kc, None)
        if not self.actions_enabled:
            return
        if self.only_xtest_sources and not self._is_xtest_source(sourceid):
            return
        is_repeat = keycode in self._pressed
        self._pressed.add(keycode)
        # 录音中按住触发键的自动重复 → 取消挂起的 release 定时器
        if keycode == self._voice_combo['trigger'] and self.voice_active:
            if self._pending_release_id:
                GLib.source_remove(self._pending_release_id)
                self._pending_release_id = 0
            return
        if is_repeat:
            return
        # 任意按键 (含修饰键单独按下) → 关活跃浮窗, 不依赖焦点
        self._close_overlay_on_key()
        if self._match_combo(self._voice_combo, keycode):
            self._on_voice_press()
        elif self._match_combo(self._screen_combo, keycode):
            GLib.idle_add(self._on_screenshot)
        elif self._match_combo(self._repeat_combo, keycode):
            self._on_repeat()

    def _handle_release(self, keycode: int, sourceid: int = 0) -> None:
        if not self._ignore_source_ids and self.ctx.guard.active():
            # 回退窗：注入键(Ctrl/V)仍忽略；热键 trigger 的抬起必须出账，否则录音卡死
            triggers = {
                self._voice_combo['trigger'],
                self._screen_combo['trigger'],
                self._repeat_combo['trigger'],
            }
            if keycode not in self._pressed or keycode not in triggers:
                return
        self._last_key_activity = time.monotonic()
        self._mod_down_since.pop(keycode, None)  # 修饰键抬起 → 从卡键账本移除
        self._pressed.discard(keycode)
        # 思路①：录音中松开热键组合的"任意一个键"(触发键或其任一修饰键)即视为结束，
        # 停录 + 清整组修饰键 —— 覆盖 Synergy 丢掉组合里某一个键 release 的情况。
        if (
            self.voice_active
            and self._is_voice_combo_key(keycode)
            and self.actions_enabled
            and (not self.only_xtest_sources or self._is_xtest_source(sourceid))
        ):
            if self._pending_release_id:
                GLib.source_remove(self._pending_release_id)
            self._pending_release_id = GLib.timeout_add(50, self._do_release)

    def _is_voice_combo_key(self, keycode: int) -> bool:
        """keycode 是否属于语音热键组合（触发键或其任一修饰键）。"""
        if keycode == self._voice_combo['trigger']:
            return True
        for mod_set in self._voice_combo['modifiers']:
            if keycode in mod_set:
                return True
        return False

    def _lingering_modifiers(self) -> dict[int, float]:
        """返回当前确实按下的修饰键 → 已持续按下秒数。

        query_keymap 为权威源；顺带对账 _mod_down_since（已抬起的移除、漏记的补记），
        因此不会因事件丢失而错乱。供看门狗与打字即时检测复用。
        """
        now = time.monotonic()
        try:
            km = self.dpy.query_keymap()
        except Exception:
            return {}
        down_now = {kc for kc in self._mod_kcs if km[kc >> 3] & (1 << (kc & 7))}
        for kc in list(self._mod_down_since):
            if kc not in down_now:
                self._mod_down_since.pop(kc, None)
        for kc in down_now:
            self._mod_down_since.setdefault(kc, now)
        return {kc: now - self._mod_down_since[kc] for kc in down_now}

    def _has_lingering_modifier(self, min_held: float) -> bool:
        """是否有修饰键确实按下且持续超过 min_held 秒（打字即时检测用，不看空闲）。"""
        return any(h > min_held for h in self._lingering_modifiers().values())

    def has_stuck_modifiers(self) -> bool:
        """看门狗用：修饰键确实按下，且 持续>HARD 或 (持续>SOFT 且 键盘空闲>IDLE)。

        绝不误伤正常长按：长按期间要么持续有按键活动(空闲小)、要么很快松开。
        """
        idle = time.monotonic() - self._last_key_activity
        for held in self._lingering_modifiers().values():
            if held > STUCK_MOD_HARD_SECONDS or (
                held > STUCK_MOD_SOFT_SECONDS and idle > STUCK_MOD_IDLE_SECONDS
            ):
                return True
        return False

    def _match_combo(self, combo: dict, pressed_kc: int | None = None) -> bool:
        """只有刚才按下的键是 trigger 才可能命中；modifier 用 query_keymap。

        禁止用 _pressed 里的陈 trigger 去配后续任意键——那是「松开后还在触发」的根因。
        修饰键位图 down 仍是权威源；幽灵卡键（持续 > STUCK_MOD_MATCH_SECONDS）不计入。
        """
        if pressed_kc is not None and pressed_kc != combo['trigger']:
            return False
        if combo['trigger'] not in self._pressed:
            return False
        if not combo['modifiers']:
            return True
        lingering = self._lingering_modifiers()  # kc → 已持续按下秒数（仅当前 down）
        saw_ghost = False
        for mod_set in combo['modifiers']:
            live = False
            for kc in mod_set:
                held = lingering.get(kc)
                if held is None:
                    continue  # 位图未按下
                if held > STUCK_MOD_MATCH_SECONDS:
                    saw_ghost = True
                    continue  # 幽灵：不计入命中
                live = True
                break
            if not live:
                if saw_ghost:
                    self._reject_ghost_modifiers(lingering)
                return False
        return True

    def _reject_ghost_modifiers(self, lingering: dict[int, float] | None = None) -> None:
        """匹配因幽灵修饰键失败时：清 XTest，并从 raw 账本剔除已超龄的修饰键。"""
        if lingering is None:
            lingering = self._lingering_modifiers()
        ghosts = [kc for kc, held in lingering.items() if held > STUCK_MOD_MATCH_SECONDS]
        if not ghosts:
            return
        print(
            f"[stuck] 热键匹配忽略幽灵修饰键 {ghosts}, 清理 XTest 层",
            flush=True,
        )
        self.ctx.janitor.clear_xtest()
        for kc in ghosts:
            self._pressed.discard(kc)
            self._mod_down_since.pop(kc, None)

    def cleanup(self) -> None:
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
        try:
            self.dpy.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# evdev 全局快捷键 (内核级，适用于 X11 和 Wayland)
# ---------------------------------------------------------------------------
