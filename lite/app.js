const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

const state = {
  recognition: null,
  recording: false,
  keyboardRecording: false,
  activeHotkey: null,
  transcript: "",
};

const recordButton = document.getElementById("recordButton");
const modeSelect = document.getElementById("mode");
const languageSelect = document.getElementById("language");
const modelInput = document.getElementById("model");
const rawText = document.getElementById("rawText");
const finalText = document.getElementById("finalText");
const statusBox = document.getElementById("status");
const copyRaw = document.getElementById("copyRaw");
const copyFinal = document.getElementById("copyFinal");

function setStatus(message) {
  statusBox.textContent = message;
}

function buildMessages(mode, text) {
  if (mode === "raw") {
    return null;
  }

  if (mode === "polish") {
    return [
      {
        role: "system",
        content:
          "你是一个中文语音输入纠错助手。请保留原意，补全标点，清理口语噪声。只输出最终文本。",
      },
      { role: "user", content: text },
    ];
  }

  return [
    {
      role: "system",
      content:
        "你是一个翻译助手。请把用户给出的中文口语转写结果翻译成自然、简洁的英文。只输出最终英文。",
    },
    { role: "user", content: text },
  ];
}

async function checkOllama() {
  try {
    const response = await fetch("http://127.0.0.1:11434/api/tags");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    setStatus("Ollama 已连接，等待录音");
  } catch (error) {
    setStatus(`Ollama 未连接: ${error.message}`);
  }
}

function initRecognition() {
  if (!Recognition) {
    setStatus("当前浏览器不支持语音识别 API，请使用 Chrome 或 Edge 并确保联网");
    return;
  }

  const recognition = new Recognition();
  recognition.lang = languageSelect.value;
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.maxAlternatives = 1;

  recognition.onstart = () => {
    state.recording = true;
    state.transcript = "";
    rawText.value = "";
    finalText.value = "";
    recordButton.classList.add("recording");
    recordButton.textContent = "松开结束";
    setStatus("录音中...");
  };

  recognition.onresult = (event) => {
    let text = "";
    for (const result of event.results) {
      text += result[0].transcript;
    }
    state.transcript = text.trim();
    rawText.value = state.transcript;
  };

  recognition.onerror = (event) => {
    setStatus(`识别失败: ${event.error}`);
  };

  recognition.onend = async () => {
    const text = state.transcript.trim();
    state.recording = false;
    recordButton.classList.remove("recording");
    recordButton.textContent = "按住说话";

    if (!text) {
      setStatus("没有捕获到语音内容");
      return;
    }

    if (modeSelect.value === "raw") {
      finalText.value = text;
      setStatus("识别完成");
      return;
    }

    await polishWithOllama(text);
  };

  state.recognition = recognition;
}

async function startRecording() {
  if (!state.recognition || state.recording) {
    return;
  }

  state.recognition.lang = languageSelect.value;
  state.recognition.start();
}

async function stopRecording() {
  if (!state.recognition || !state.recording) {
    return;
  }

  setStatus("录音结束，发送给 Ollama...");
  state.recognition.stop();
}

async function polishWithOllama(text) {
  const messages = buildMessages(modeSelect.value, text);
  if (!messages) {
    finalText.value = text;
    return;
  }

  const payload = {
    model: modelInput.value.trim(),
    messages,
    stream: false,
    options: { temperature: 0.1 },
  };

  try {
    const response = await fetch("http://127.0.0.1:11434/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    finalText.value = data.message?.content?.trim() || "";
    setStatus("处理完成");
  } catch (error) {
    setStatus(`Ollama 调用失败: ${error.message}`);
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

copyRaw.addEventListener("click", () => copyText(rawText.value));
copyFinal.addEventListener("click", () => copyText(finalText.value));

initRecognition();
checkOllama();
