const state = {
  recorder: null,
  stream: null,
  chunks: [],
  recording: false,
  keyboardRecording: false,
  activeHotkey: null,
  recordStartTime: 0,
};

const recordButton = document.getElementById("recordButton");
const statusBox = document.getElementById("status");
const modeSelect = document.getElementById("mode");
const languageSelect = document.getElementById("language");
const rawText = document.getElementById("rawText");
const finalText = document.getElementById("finalText");
const copyRaw = document.getElementById("copyRaw");
const copyFinal = document.getElementById("copyFinal");

function setStatus(message) {
  statusBox.textContent = message;
}

function preferredMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
  ];

  for (const type of candidates) {
    if (MediaRecorder.isTypeSupported(type)) {
      return type;
    }
  }

  return "";
}

async function loadStatus() {
  try {
    const response = await fetch("/api/status");
    const data = await response.json();
    const ollamaState = data.ollama_ok ? "已连接" : "未连接";
    let statusText = `Whisper: ${data.whisper_model} | Ollama: ${data.ollama_model} (${ollamaState})`;
    if (data.vision_ok === false) {
      statusText += ` | 截图翻译: 缺少 ${data.vision_model}`;
    }
    setStatus(statusText);
  } catch (error) {
    setStatus(`状态读取失败: ${error.message}`);
  }
}

async function ensureStream() {
  if (state.stream) {
    return state.stream;
  }

  state.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  return state.stream;
}

async function startRecording() {
  if (state.recording) {
    return;
  }

  try {
    const stream = await ensureStream();
    state.chunks = [];
    const mimeType = preferredMimeType();
    const options = mimeType ? { mimeType } : undefined;
    state.recorder = new MediaRecorder(stream, options);
    state.recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        state.chunks.push(event.data);
      }
    };
    state.recorder.start(250);
    state.recording = true;
    state.recordStartTime = Date.now();
    recordButton.classList.add("recording");
    recordButton.textContent = "松开结束";
    setStatus("录音中...");
  } catch (error) {
    setStatus(`无法开始录音: ${error.message}`);
  }
}

async function stopRecording() {
  if (!state.recording || !state.recorder) {
    return;
  }

  const duration = Date.now() - state.recordStartTime;
  const recorder = state.recorder;
  state.recording = false;
  state.recorder = null;
  recordButton.classList.remove("recording");
  recordButton.textContent = "按住说话";

  if (duration < 500) {
    setStatus("录音太短，请按住至少 1 秒");
    recorder.stop();
    return;
  }

  setStatus("录音结束，处理中...");

  await new Promise((resolve) => {
    recorder.onstop = resolve;
    recorder.stop();
  });

  const blob = new Blob(state.chunks, {
    type: recorder.mimeType || "audio/webm",
  });

  if (blob.size < 1000) {
    setStatus("未捕获到有效音频，请检查麦克风权限");
    return;
  }

  await submitAudio(blob);
}

async function submitAudio(blob) {
  const formData = new FormData();
  formData.append("audio", blob, "recording.webm");
  formData.append("mode", modeSelect.value);
  formData.append("language", languageSelect.value);

  try {
    const response = await fetch("/api/transcribe", {
      method: "POST",
      body: formData,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "转写失败");
    }

    rawText.value = data.raw_text || "";
    finalText.value = data.final_text || "";
    setStatus("处理完成");
  } catch (error) {
    const msg = error.message || "";
    if (msg.includes("没有识别到语音") || msg.includes("没有识别到"))
      setStatus("没有识别到语音，请对准麦克风清晰说话后重试");
    else if (msg.includes("Ollama"))
      setStatus("Ollama 服务未连接，请检查是否已启动");
    else if (msg.includes("音频文件太小"))
      setStatus("录音时间不足，请按住按钮说话再松开");
    else setStatus(`处理失败: ${msg}`);
  }
}

async function copyText(value) {
  if (!value) {
    return;
  }

  try {
    await navigator.clipboard.writeText(value);
    setStatus("已复制到剪贴板");
  } catch (error) {
    setStatus(`复制失败: ${error.message}`);
  }
}

recordButton.addEventListener("pointerdown", async (event) => {
  event.preventDefault();
  await startRecording();
});

recordButton.addEventListener("pointerup", async (event) => {
  event.preventDefault();
  await stopRecording();
});

recordButton.addEventListener("pointerleave", async () => {
  if (state.recording) {
    await stopRecording();
  }
});

copyRaw.addEventListener("click", () => copyText(rawText.value));
copyFinal.addEventListener("click", () => copyText(finalText.value));

document.addEventListener("keydown", async (event) => {
  if (event.repeat) {
    return;
  }

  const target = event.target;
  const typing =
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target?.isContentEditable;
  if (typing) {
    return;
  }

  let hotkey = null;
  if (event.code === "Space") {
    hotkey = "space";
  } else if (event.ctrlKey && event.code === "Backquote") {
    hotkey = "ctrl-backquote";
  }

  if (!hotkey || state.keyboardRecording) {
    return;
  }

  event.preventDefault();
  state.keyboardRecording = true;
  state.activeHotkey = hotkey;
  await startRecording();
});

document.addEventListener("keyup", async (event) => {
  let hotkey = null;
  if (event.code === "Space") {
    hotkey = "space";
  } else if (event.code === "Backquote" || event.code === "ControlLeft" || event.code === "ControlRight") {
    hotkey = "ctrl-backquote";
  }

  if (!state.keyboardRecording || !hotkey || hotkey !== state.activeHotkey) {
    return;
  }

  event.preventDefault();
  state.keyboardRecording = false;
  state.activeHotkey = null;
  await stopRecording();
});

loadStatus();
