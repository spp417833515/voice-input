"""真实注入后端：把已定稿文本送到当前焦点窗口，并在粘贴后归还用户原剪贴板。

焦点比对在 `_inject.FocusBoundInjector`（按 job 绑定的焦点）完成，本模块只负责"怎么打出去"。
证据规则：
- IBus 直接提交成功 / 粘贴按键已完整发出 → proved_output
- 剪贴板写入失败或所有按键通道都不可用（一个键都没发）→ proved_none
- 按键发送途中异常 → 无法证明，partial_or_unknown

输入层级（迁自旧 daemon._type_text，语义不变）：
Tier 1: IBus commit_text —— 输入法框架直接提交（最可靠，无剪贴板副作用）
Tier 2: uinput —— 内核级独立虚拟设备（不触碰用户物理修饰键，首选粘贴方式）
Tier 3: xdotool / wtype —— 子进程合成粘贴键（回退，检查返回码）
Tier 4: pynput —— XTest 合成按键（最后回退）
"""
from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable

from _inject import InjectEvidence
from _linux.clipboard import Clipboard
from _linux.ibus import IBusCommitter
from _linux.inject_guard import InjectGuard
from _linux.modifiers import ModifierJanitor


class _PasteInterrupted(RuntimeError):
    """已经开始发送按键后失败：不能证明没有输出。"""


class LinuxPasteBackend:
    def __init__(
        self,
        *,
        session_type: str,
        guard: InjectGuard,
        janitor: ModifierJanitor,
        clipboard: Clipboard,
        ibus: IBusCommitter | None = None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session_type = session_type
        self.guard = guard
        self.janitor = janitor
        self.clipboard = clipboard
        self.ibus = ibus or IBusCommitter()
        self._run = run
        self._sleep = sleep
        self._xdotool = which("xdotool")
        self._wtype = which("wtype")

    # ------------------------------------------------------------------
    def deliver(self, text: str, *, terminal: bool = False) -> InjectEvidence:
        """全程持有注入锁（与重复输入/看门狗互斥），注入期间打开回流保护窗。"""
        with self.guard.lock:
            self.guard.open_window(3.0)  # 注入期间抓取器忽略回流按键
            try:
                self._sleep(0.1)  # 等待焦点稳定
                if self.ibus.commit(text):
                    print(f"  输入: Tier1 IBus 直接提交成功 ({len(text)} 字)", flush=True)
                    return InjectEvidence(True, True, False, method="ibus", detail="已输入")
                return self._paste_with_restore(text, terminal=terminal)
            finally:
                self.guard.open_window(0.2)  # 注入尾窗，覆盖回流事件延迟

    def _paste_with_restore(self, text: str, *, terminal: bool) -> InjectEvidence:
        """先存用户剪贴板 → 写入本次文本 → 粘贴 → 仅在剪贴板仍是本次结果时恢复，避免覆盖用户新复制的内容。"""
        old_clipboard = self.clipboard.read()
        error = self._write_verified(text)
        if error is not None:
            return InjectEvidence(False, False, True, method="clipboard", detail=error)
        try:
            method = self._send_paste(terminal=terminal)
        except _PasteInterrupted as exc:
            self._restore(old_clipboard, text)
            return InjectEvidence(False, False, False, method="paste", detail=str(exc))
        if method is None:
            self._restore(old_clipboard, text)
            print("  输入: [ERROR] 所有输入方式均不可用！", flush=True)
            return InjectEvidence(
                False, False, True, method="none", detail="无法模拟粘贴：xdotool/uinput/pynput 均不可用"
            )
        self._sleep(0.3)
        self._restore(old_clipboard, text)
        return InjectEvidence(True, True, False, method=method, detail="已发送粘贴")

    def _write_verified(self, text: str) -> str | None:
        error = self.clipboard.write(text)
        if error is not None:
            return error
        # xclip → X11 CLIPBOARD → Mutter 桥接 → Wayland 剪贴板，需要足够延迟
        self._sleep(0.3)
        check = self.clipboard.read()
        if check == text:
            print(f"  输入: 剪贴板已写入 ({len(text)} 字) ✓", flush=True)
            return None
        print(
            f"  输入: [WARN] 剪贴板验证失败! 期望 {len(text)} 字, 实际 {len(check) if check else 0} 字",
            flush=True,
        )
        error = self.clipboard.write(text)
        if error is not None:
            return f"剪贴板重试写入失败：{error}"
        self._sleep(0.3)
        return None

    def _restore(self, old_clipboard: str, text: str) -> None:
        if self.clipboard.read() == text:
            self.clipboard.write(old_clipboard)

    # ------------------------------------------------------------------
    def _send_paste(self, *, terminal: bool) -> str | None:
        """返回成功的粘贴方式名；一个键都没发出返回 None；发送途中失败抛 _PasteInterrupted。"""
        print(f"  输入: 使用剪贴板粘贴 (终端={terminal}, 会话={self.session_type})", flush=True)
        # 注入前等用户物理修饰键真正松开，避免与按住的 Ctrl/Alt 组合成误触发
        self.janitor.wait_released()
        if self.session_type == "wayland":
            # Wayland 无法检测焦点窗口类型，始终 Ctrl+Shift+V（终端需要 Shift，其他应用视为"粘贴纯文本"）
            if self._uinput_paste(True):
                return "uinput"
            if self._wtype and self._run_ok(
                [self._wtype, "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]
            ):
                return "wtype"
        else:
            # X11: 优先 uinput（独立设备，无 --clearmodifiers 重按竞态）
            if self._uinput_paste(terminal):
                return "uinput"
            # X11 回退: xdotool（不用 --clearmodifiers；注入前已等修饰键松开；查返回码）
            if self._xdotool:
                combo = "ctrl+shift+v" if terminal else "ctrl+v"
                if self._run_ok([self._xdotool, "key", combo]):
                    return "xdotool"
        return self._pynput_paste(terminal)

    def _run_ok(self, cmd: list[str]) -> bool:
        try:
            r = self._run(cmd, timeout=3, check=False, capture_output=True)
        except Exception as exc:
            print(f"  输入: {cmd[0]} 异常 {exc}, 回退下一级", flush=True)
            return False
        if r.returncode != 0:
            print(f"  输入: {cmd[0]} 返回码 {r.returncode}, 回退下一级", flush=True)
            return False
        return True

    def _uinput_paste(self, is_term: bool) -> bool:
        """用 uinput 合成 Ctrl(+Shift)+V 粘贴，成功返回 True。

        press→syn→release→syn 全程配对，finally 幂等清零并补一次 syn，确保 release 帧真正被
        内核消费（否则 release 滞留内核队列，消费者视角按键持续卡住）。
        """
        uinput = self.janitor.uinput
        if not uinput:
            return False
        from evdev import ecodes

        seq: list[int] = [ecodes.KEY_LEFTCTRL]
        if is_term:
            seq.append(ecodes.KEY_LEFTSHIFT)
        seq.append(ecodes.KEY_V)
        pressed: list[int] = []
        ok = False
        try:
            for code in seq:
                uinput.write(ecodes.EV_KEY, code, 1)
                pressed.append(code)
            uinput.syn()
            self._sleep(0.03)
            uinput.write(ecodes.EV_KEY, ecodes.KEY_V, 0)
            ok = True
        except Exception as exc:
            print(f"  输入: uinput 粘贴失败: {exc}", flush=True)
            if pressed:
                raise _PasteInterrupted(f"uinput 粘贴中途失败: {exc}") from exc
        finally:
            for code in reversed(pressed):
                try:
                    uinput.write(ecodes.EV_KEY, code, 0)
                except Exception:
                    pass
            try:
                uinput.syn()
            except Exception as exc:
                print(f"  输入: uinput release syn 失败: {exc}", flush=True)
            self.janitor.clear_uinput()
        return ok

    def _pynput_paste(self, terminal: bool) -> str | None:
        kb, Key = self.janitor.kb, self.janitor.Key
        if kb is None:
            return None
        pressed_keys: list = [Key.ctrl]
        if terminal:
            pressed_keys.append(Key.shift)
        held: list = []
        try:
            for k in pressed_keys:
                kb.press(k)
                held.append(k)
            kb.tap("v")
        except Exception as exc:
            print(f"  输入: Tier4 pynput 失败: {exc}", flush=True)
            if held:
                raise _PasteInterrupted(f"pynput 粘贴中途失败: {exc}") from exc
            return None
        finally:
            for k in reversed(held):
                try:
                    kb.release(k)
                except Exception:
                    pass
            self.janitor.clear_pynput()
        return "pynput"
