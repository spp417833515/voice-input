<div align="center">

# 🎙️ Ollama Voice Input

**全局语音输入 + 截图翻译 — 完全本地，不依赖任何云服务**

按住快捷键说话 → Whisper 转文字 → Ollama 润色 → 自动输入到任意应用

[![Version](https://img.shields.io/badge/Version-2.0-blue?style=flat-square)](https://github.com/spp417833515/voice-input)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://python.org)
[![Ollama](https://img.shields.io/badge/Ollama-本地LLM-000000?logo=ollama)](https://ollama.com)
[![Whisper](https://img.shields.io/badge/Faster--Whisper-语音识别-green)](https://github.com/SYSTRAN/faster-whisper)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux%20(X11)-FCC624?logo=linux&logoColor=black)](https://kernel.org)

</div>

---

## ✨ 特性

🗣️ **全局语音输入** — 在任意应用中按住快捷键说话，松开后自动将文字输入到光标位置

📸 **截图翻译** — 框选屏幕任意区域，OCR 识别 + 翻译，结果直接浮窗显示在原位

🔒 **完全离线** — Whisper 语音识别 + Ollama 大模型，全部本地运行，隐私安全

⚡ **连续录音不丢失** — 队列化处理，上一条还没处理完就可以开始下一条

🎵 **录音提示音** — 开始/结束录音有明确的音频反馈，不会漏录

🖥️ **悬浮状态栏** — 常驻桌面顶部，实时显示录音/处理/就绪状态

🌐 **Web 界面** — 浏览器中也能用，提供录音、历史记录、模型管理等完整功能

🪶 **零依赖轻量版** — 不装任何 Python 包也能用，浏览器原生语音识别 + Ollama 润色

### v2.0 新增

🔄 **三级输入策略** — IBus → 智能剪贴板 → pynput 三级递降，确保微信、QQ、终端、IDE 全场景可用

⌨️ **可配置快捷键** — 在 `.config.json` 中自定义所有快捷键，无需改代码

🔁 **重复输入** — 一键重复上次语音识别结果，无需再次录音

🎯 **截图响应优化** — maim 进程在主线程预启动，消除线程调度延迟

---

## 🎬 工作原理

```
按住 Ctrl+`
    │
    ▼
┌──────────────────┐
│   麦克风录音       │  ← sounddevice 实时采集
│   (按住期间持续)    │
└────────┬─────────┘
         │ 松开快捷键
         ▼
┌──────────────────┐
│  Faster-Whisper   │  ← 本地语音转文字 (支持 GPU 加速)
│  语音识别 (STT)    │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   Ollama LLM      │  ← 纠错、加标点、整理口语 / 翻译英文
│   文本润色/翻译    │
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────────┐
│   三级输入策略（自动递降）              │
│                                      │
│  1️⃣ IBus commit_text  ← 输入法级别    │
│     ↓ 失败                            │
│  2️⃣ 智能剪贴板粘贴    ← uinput 按键   │
│     ↓ 失败                            │
│  3️⃣ pynput 合成按键   ← X11 兜底      │
└──────────────────────────────────────┘
```

---

## 🚀 快速开始

### 前置条件

- **Linux** (X11 桌面环境)
- **Python 3.10+**
- **Ollama** — [安装指南](https://ollama.com/download/linux)
- 系统工具：`maim`（截图）、`xclip`（剪贴板）

```bash
# Ubuntu / Debian
sudo apt install maim xclip

# Arch / Manjaro
sudo pacman -S maim xclip
```

### 方式一：一键启动（推荐）

```bash
git clone https://github.com/spp417833515/voice-input.git
cd ollama-voice-input
chmod +x start.sh
./start.sh
```

`start.sh` 会自动完成：创建虚拟环境 → 安装依赖 → 检查/启动 Ollama → 拉取模型 → 启动服务。

### 方式二：手动启动

```bash
# 1. 创建虚拟环境 & 安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 确保 Ollama 已运行
ollama serve &
ollama pull qwen2.5:7b        # 语音润色模型
ollama pull moondream:1.8b     # 截图翻译视觉模型

# 3. 启动 API 服务
uvicorn app:app --host 127.0.0.1 --port 17945 &

# 4. 启动全局快捷键守护进程
python daemon.py
```

### 方式三：零依赖轻量版

只需要 Ollama，不需要安装任何 Python 包：

```bash
ollama serve &
ollama pull qwen2.5:3b
python3 -m http.server 8000
# 打开 http://127.0.0.1:8000/lite/
```

> 💡 轻量版使用浏览器内置语音识别（Web Speech API），Ollama 只负责润色。适合快速体验。

---

## ⌨️ 快捷键

### 默认快捷键

| 快捷键 | 功能 | 说明 |
|:---:|:---|:---|
| `Ctrl+`` | **语音输入** | 按住说话，松开后自动转写并输入 |
| `Alt+X` | **截图翻译** | 框选区域，OCR + 翻译，浮窗显示 |
| `Ctrl+Shift+Z` | **重复输入** | 重新输入上次的识别结果 |
| `Esc` | 关闭翻译浮窗 | 或点击浮窗外部自动关闭 |

> 快捷键通过 X11 `XGrabKey` 注册，在任何应用中都可使用（真正的全局热键）。

### 自定义快捷键

所有快捷键均可通过 `.config.json` 配置：

```json
{
  "hotkey_voice": "ctrl+grave",
  "hotkey_screenshot": "alt+x",
  "hotkey_repeat": "ctrl+shift+z"
}
```

**格式说明**：修饰键 + 按键名，用 `+` 连接。支持的修饰键：`ctrl`、`alt`、`shift`。
按键名使用 X11 keysym 名称（如 `grave` = `` ` ``，`space`、`f1` 等）。

修改后重启守护进程即可生效。

---

## 🗣️ 语音模式

| 模式 | 说明 | 适用场景 |
|:---|:---|:---|
| `raw` | 原始转写，不做后处理 | 需要逐字记录 |
| `polish` | 纠错 + 加标点 + 整理口语 | **日常使用（默认）** |
| `translate_en` | 中文语音 → 英文输出 | 跨语言沟通 |

可在 Web 设置页面 (`http://127.0.0.1:17945/setup`) 切换模式。

---

## 🔌 三级输入策略

v2.0 引入智能三级递降输入策略，确保文字能正确输入到任何应用：

| 级别 | 方式 | 原理 | 适用应用 |
|:---|:---|:---|:---|
| **Tier 1** | IBus `commit_text` | 输入法级别提交，不经过剪贴板 | 支持 IBus 的所有应用 |
| **Tier 2** | 智能剪贴板粘贴 | 自动识别终端(`Ctrl+Shift+V`) / 普通应用(`Ctrl+V`)，使用 uinput 内核按键 | 所有 GUI 应用 |
| **Tier 3** | pynput 合成按键 | X11 XTest 合成按键作为兜底 | 最大兼容性 |

**终端自动识别**：支持 26 种主流终端（gnome-terminal、konsole、alacritty、kitty、tilix、xterm 等），自动使用终端专用快捷键 `Ctrl+Shift+V`。

---

## 📁 项目结构

```
ollama-voice-input/
├── app.py               # FastAPI 后端 (Whisper + Ollama API)
├── daemon.py            # 桌面守护进程 (全局热键 + 悬浮状态栏)
├── screenshot_ui.py     # 独立截图翻译工具
├── start.sh             # 一键启动脚本
├── install.sh           # 开机自启安装脚本
├── restart.sh           # 快速重启脚本
├── requirements.txt     # Python 依赖
├── .config.json         # 运行时配置（快捷键、语音模式等）
├── .env.example         # 环境变量参考
├── static/
│   ├── index.html       # Web 主界面
│   ├── app.js           # 前端录音逻辑
│   ├── setup.html       # 配置管理页面
│   └── history.html     # 转写历史记录
├── lite/
│   ├── index.html       # 零依赖轻量版
│   └── app.js           # 轻量版逻辑
└── systemd/
    ├── ollama-voice-server.service
    └── ollama-voice-daemon.service
```

---

## ⚙️ 配置

### 环境变量

在 `.env` 文件中配置（参考 `.env.example`）：

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `WHISPER_MODEL` | `small` | Whisper 模型大小 (`small` / `medium` / `large-v3` / `large-v3-turbo`) |
| `WHISPER_DEVICE` | `auto` | 推理设备 (`auto` / `cpu` / `cuda`) |
| `WHISPER_LANGUAGE` | `zh` | 识别语言 (`zh` / `en` / `ja` / `auto`) |
| `OLLAMA_MODEL` | `qwen2.5:7b` | 文本润色/翻译使用的模型 |
| `VISION_MODEL` | `moondream:1.8b` | 截图翻译使用的视觉模型 |
| `VOICE_MODE` | `polish` | 语音模式 (`raw` / `polish` / `translate_en`) |

### Whisper 模型选择

| 模型 | 大小 | 速度 | 中文准确率 | 建议 |
|:---|:---|:---|:---|:---|
| `small` | ~460 MB | ⚡⚡⚡ | ★★★☆ | 日常使用，速度优先 |
| `medium` | ~1.5 GB | ⚡⚡ | ★★★★ | 平衡之选 |
| `large-v3-turbo` | ~1.6 GB | ⚡⚡ | ★★★★☆ | 推荐，准确且快 |
| `large-v3` | ~3 GB | ⚡ | ★★★★★ | 最高准确率 |

### 自定义关键词

在设置页面可添加领域专业词汇，提高 Whisper 对特定术语的识别准确率。

---

## 🖥️ 开机自启

```bash
chmod +x install.sh
./install.sh
```

安装脚本会：
1. 注册 systemd 用户服务（开机自动启动）
2. 创建桌面快捷方式
3. 添加到应用菜单

管理命令：

```bash
# 启动/停止
systemctl --user start ollama-voice-server
systemctl --user start ollama-voice-daemon

# 查看状态
systemctl --user status ollama-voice-server

# 卸载
systemctl --user disable --now ollama-voice-server
systemctl --user disable --now ollama-voice-daemon
```

---

## 🔌 API 参考

服务默认运行在 `http://127.0.0.1:17945`。

| 端点 | 方法 | 说明 |
|:---|:---|:---|
| `/api/transcribe` | POST | 语音转写 + 润色 |
| `/api/ocr_translate` | POST | 截图 OCR + 翻译 |
| `/api/status` | GET | 服务状态 & Whisper/Ollama 可用性 |
| `/api/history` | GET | 转写历史记录 |
| `/api/setup/check` | GET | 环境检查（Ollama、模型列表） |
| `/api/setup/pull_model` | POST | 拉取 Ollama 模型 |
| `/api/setup/set_config` | POST | 更新配置 |

---

## 🔧 微信/QQ/终端 兼容

v2.0 的三级输入策略确保在所有应用中正确输入：

1. **IBus 输入法直接提交** — 最干净的方式，不污染剪贴板
2. **uinput 内核级按键** — 与物理键盘按键完全一致，微信/QQ 无法区分
3. **终端智能识别** — 自动检测 26 种终端，使用 `Ctrl+Shift+V` 而非 `Ctrl+V`

首次使用需要设置 uinput 权限：

```bash
# 临时（重启后失效）
sudo chmod 666 /dev/uinput

# 永久（推荐）
echo 'KERNEL=="uinput", MODE="0666"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules
```

启动时看到 `uinput: 已初始化（内核级按键模式）` 表示配置成功。如果 uinput 不可用，会自动回退到 pynput（X11 合成按键）。

---

## 💡 常见问题

<details>
<summary><b>Q: 第一次使用很慢？</b></summary>

首次转写时会自动下载 Whisper 模型，根据选择的模型大小，可能需要几分钟。后续使用会直接从本地缓存加载。
</details>

<details>
<summary><b>Q: Ollama 没启动会怎样？</b></summary>

`raw` 模式仍然可用（直接输出 Whisper 识别结果）。`polish` 和 `translate_en` 模式需要 Ollama。
</details>

<details>
<summary><b>Q: 快捷键没反应？</b></summary>

1. 确认守护进程在运行：检查悬浮状态栏是否显示
2. 某些桌面环境可能占用了默认快捷键，可在 `.config.json` 中修改
3. 守护进程有 500ms 自动刷新机制，一般稍等即可恢复
</details>

<details>
<summary><b>Q: 支持 Wayland 吗？</b></summary>

当前版本依赖 X11（XGrabKey、XTest），暂不支持 Wayland。剪贴板操作已兼容 Wayland（自动检测并使用 wl-copy）。
</details>

<details>
<summary><b>Q: 如何使用 GPU 加速？</b></summary>

安装 CUDA 版本的 PyTorch，然后设置 `WHISPER_DEVICE=cuda`。`start.sh` 会自动检测并设置 CUDA 库路径。
</details>

<details>
<summary><b>Q: 如何修改快捷键？</b></summary>

编辑项目目录下的 `.config.json` 文件，修改 `hotkey_voice`、`hotkey_screenshot`、`hotkey_repeat` 字段。格式为 `修饰键+按键名`，例如 `"ctrl+space"`、`"alt+z"`。修改后重启守护进程。
</details>

---

## 🏗️ 技术栈

| 组件 | 技术 | 作用 |
|:---|:---|:---|
| 语音识别 | [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) | 本地 STT，支持 GPU 加速 |
| 文本处理 | [Ollama](https://ollama.com) | 本地 LLM，润色/翻译 |
| 后端框架 | [FastAPI](https://fastapi.tiangolo.com) + Uvicorn | 异步 HTTP API |
| 桌面集成 | GTK3 + python-xlib + IBus | 全局热键、悬浮窗、输入法集成 |
| 音频采集 | sounddevice + NumPy | 实时麦克风录音 |
| 按键模拟 | evdev (uinput) / pynput | 内核级/X11 级键盘输入 |
| 截图工具 | maim + xclip | 屏幕选区截图 |
| OCR 引擎 | Tesseract + Google Translate | 屏幕文字识别 + 翻译 |

---

## 📝 更新日志

### v2.0 (2026-04-02)
- **三级输入策略** — IBus → 智能剪贴板 → pynput 递降，彻底解决微信/QQ/终端输入兼容问题
- **可配置快捷键** — `.config.json` 中自定义 `hotkey_voice`、`hotkey_screenshot`、`hotkey_repeat`
- **重复输入** — `Ctrl+Shift+Z` 一键重复上次识别结果
- **截图延迟优化** — maim 进程主线程预启动，消除线程调度延迟
- **录音提示音** — 开始(400Hz)/结束(700Hz) 低延迟正弦波提示
- **悬浮状态栏优化** — 更大字体(14px)、更宽间距、更强可读性
- **翻译浮窗焦点检测** — 点击浮窗外部自动关闭

### v1.0 (2026-03-28)
- 全局语音输入 (`Ctrl+``)
- 截图翻译 (`Alt+X`)
- Web 界面 + 零依赖轻量版
- Whisper 语音识别 + Ollama 文本润色
- 悬浮状态栏 + 连续录音队列
- 一键启动脚本 + systemd 开机自启

---

## 📄 License

[MIT](LICENSE) — 自由使用、修改和分发。
