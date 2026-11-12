/* 仅供测试 / 预览使用的示例数据，不得当作生产页默认状态。 */
(function (global) {
  "use strict";

  const RAW = "今天下午三点我们在会议室讨论一下新版本的发布计化，嗯，还有千问的接入方案";
  const POLISHED = "今天下午三点，我们在会议室讨论一下新版本的发布计划，还有 Qwen 的接入方案。";

  global.V3Fixtures = {
    RAW: RAW,
    POLISHED: POLISHED,
    setupConnected: {
      connection: "connected",
      aboutText: "本地运行（测试夹具）",
      asr: {
        name: "Qwen3-ASR-1.7B",
        status: "ready",
        sizeLabel: "3.5 GB",
        deviceLabel: "GPU 加速",
        action: null
      },
      ollama: {
        modelName: "qwen3.5:4b",
        installed: true,
        running: true,
        version: "0.12.6",
        modelStatus: "ready",
        sizeLabel: "3.4 GB",
        action: null
      },
      polish_enabled: true,
      custom_keywords: ["Qwen", "Ollama"],
      translate_target_lang: "zh",
      hotkey_voice: "ctrl+alt+z",
      hotkey_screenshot: "ctrl+alt+x",
      hotkey_repeat: "ctrl+alt+r",
      lanEntries: [{ url: "https://example.invalid/voice?token=test", qrSvg: null }],
      tokenMasked: "a8f3••••7c21",
      tokenPresent: true,
      serviceUrl: "https://example.invalid",
      localUrl: "https://127.0.0.1:8765",
      history: {
        records: [
          {
            timestamp: "11-09 10:42",
            raw_text: RAW,
            final_text: POLISHED,
            language: "zh",
            source: "desktop",
            polish: "on",
            job_id: "job-1"
          },
          {
            timestamp: "11-09 10:37",
            raw_text: "把这个函数拆成两个，一个负责读取一个负责校验",
            final_text: "把这个函数拆成两个，一个负责读取一个负责校验",
            language: "zh",
            source: "web",
            polish: "failed",
            job_id: "job-2"
          }
        ],
        total: 2,
        skipped_malformed: 0
      },
      capabilities: [
        { name: "portal 截图（Wayland）", available: true },
        { name: "xclip（剪贴板）", available: false, installCommand: "sudo apt install xclip" }
      ]
    },
    setupError: {
      connection: "error",
      connectionError: {
        ok: false,
        error: "dependency_failed",
        message: "connect ECONNREFUSED 127.0.0.1:8765"
      }
    },
    setupDownloading: {
      connection: "connected",
      asr: {
        status: "downloading",
        progress: { receivedLabel: "1.47 GB", totalLabel: "3.50 GB", percent: 42 },
        action: "download"
      },
      ollama: { installed: true, running: true, modelStatus: "ready" }
    },
    setupFailedDownload: {
      connection: "connected",
      asr: {
        status: "failed",
        message: "下载中断：连接被重置（测试夹具，不是成功）",
        action: "retry"
      },
      ollama: {
        installed: true,
        running: false,
        modelStatus: "missing",
        message: "Ollama 未运行",
        action: "start"
      }
    },
    statusPartial: {
      connection: "connected",
      events: [
        {
          event: "partial",
          session_id: "s-1",
          stable_text: "今天下午三点我们在会议室讨论",
          tail_text: "一下新版"
        }
      ]
    },
    statusUnknown: {
      connection: "connected",
      hotkey_repeat: "ctrl+alt+r",
      events: [
        { event: "delivery_unknown", job_id: "job-9", text: POLISHED }
      ]
    },
    statusError: {
      connection: "connected",
      hotkey_repeat: "ctrl+alt+r",
      events: [
        { event: "error", error: "asr_unavailable", message: "语音模型未就绪" }
      ]
    },
    translateResult: {
      connection: "connected",
      phase: "result",
      text: "版本说明 3.0",
      ocr_text: "Release notes 3.0",
      target_lang: "zh"
    },
    translateError: {
      connection: "connected",
      phase: "error",
      error: "translate_failed",
      message: "本地模型无响应（Ollama 未运行）。",
      ocr_text: "Release notes 3.0",
      text: ""
    },
    voicePolishFail: {
      connection: "connected",
      tokenValid: true,
      micEnabled: true,
      phase: "polish_failed",
      error: "polish_failed",
      message: "本地模型无响应。",
      raw_text: "把这个函数拆成两个，一个负责读取一个负责校验",
      text: "把这个函数拆成两个，一个负责读取一个负责校验",
      polish: "failed",
      history: { records: [], total: 0, skipped_malformed: 0 }
    }
  };
})(typeof window !== "undefined" ? window : global);
