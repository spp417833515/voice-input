"""真实麦克风源（sounddevice / PortAudio）与提示音。只在 daemon 生产路径使用。"""
from __future__ import annotations

import sys
import threading

from _capture import SAMPLE_RATE, PcmSink


class SoundDeviceSource:
    """16k 单声道 int16 输入流；回调里只把字节交给 sink（SessionCapture 负责切块）。"""

    def __init__(self, *, samplerate: int = SAMPLE_RATE) -> None:
        self.samplerate = samplerate
        self._stream = None

    def start(self, sink: PcmSink) -> None:
        import sounddevice as sd

        def callback(indata, frames, time_info, status):
            sink(bytes(indata))

        stream = sd.RawInputStream(
            samplerate=self.samplerate, channels=1, dtype="int16", callback=callback
        )
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self._stream = stream

    def stop(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        # 流关闭异常（设备热拔 / PipeWire 重启）绝不能向上抛：否则调用方其后的修饰键释放被跳过
        # = 修饰键卡住，且麦克风流句柄泄漏。异常吞掉但打印。
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            print(f"[mic] 关闭音频流异常: {exc}", file=sys.stderr)


def play_beep(freq: float = 440, duration: float = 0.1, volume: float = 0.3) -> None:
    """播放简短提示音。整体放后台线程执行。

    sd.play 内部同步打开输出流，设备忙时可阻塞数百毫秒~数秒；beep 从按键回调（GTK 主线程）
    触发，一旦阻塞主循环，按键处理全部延迟 → 用户体感"按键卡住"。故整体移出主线程。
    """
    def _worker() -> None:
        try:
            import numpy as np
            import sounddevice as sd

            t = np.linspace(0, duration, int(SAMPLE_RATE * duration), endpoint=False)
            wave = volume * np.sin(2 * np.pi * freq * t).astype(np.float32)
            fade = min(int(SAMPLE_RATE * 0.01), len(wave) // 4)
            wave[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
            wave[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
            sd.play(wave, SAMPLE_RATE)
        except Exception:
            pass  # 提示音非关键功能，失败不影响录音
    threading.Thread(target=_worker, daemon=True).start()
