<div align="center">

# 🎙️ Ollama Voice Input

**全局语音输入 + 截图翻译 — 完全本地，不依赖任何云服务**

按住快捷键说话 → Qwen3-ASR 流式识别 → 本地 qwen3.5:4b 纠错 → 自动输入到任意应用

[![Version](https://img.shields.io/badge/Version-3.0-blue?style=flat-square)](https://github.com/spp417833515/voice-input)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://python.org)
[![Ollama](https://img.shields.io/badge/Ollama-本地LLM-000000?logo=ollama)](https://ollama.com)
[![Qwen3-ASR](https://img.shields.io/badge/Qwen3--ASR-语音识别-green)](https://github.com/QwenLM/Qwen3-ASR)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux-FCC624?logo=linux&logoColor=black)](https://kernel.org)

</div>

---

## ✨ 特性

🗣️ **全局语音输入** — 在任意应用中按住快捷键说话，松开后自动将文字输入到光标位置

📸 **截图翻译** — 框选屏幕任意区域，OCR 识别 + 翻译，结果直接浮窗显示在原位

🔒 **完全离线** — Qwen3-ASR 语音识别 + Ollama qwen3.5:4b 纠错/翻译，全部本地运行，不调用任何在线翻译

⚡ **连续录音不丢失** — 队列化处理，上一条还没处理完就可以开始下一条

🎵 **录音提示音** — 开始/结束录音有明确的音频反馈，不会漏录

🖥️ **悬浮状态栏** — 常驻桌面左下角，实时显示录音字幕/纠错/已输入/错误（与网页同一事件流）

🌐 **Web 界面** — https 设置页一键安装模型、查看历史；局域网手机扫码即可语音录入到电脑

🪶 **零依赖轻量版** — 不装任何 Python 包也能用，浏览器原生语音识别 + Ollama 润色

### v2.0 新增

🔄 **三级输入策略** — IBus → 智能剪贴板 → pynput 三级递降，确保微信、QQ、终端、IDE 全场景可用

⌨️ **可配置快捷键** — 在 `.config.json` 中自定义所有快捷键，无需改代码

🔁 **重复输入** — 一键重复上次语音识别结果，无需再次录音

🎯 **截图链路根治** — Wayland 走 xdg-desktop-portal 整屏 + 自绘冻结选区；X11 保留 maim

---

## 🎬 工作原理

```
按住 Ctrl+Alt+Z ──► 记下本句的焦点窗口 + job_id
    │
    ▼  每 0.5s 一块 PCM（sounddevice）
┌──────────────────────────────┐
│ /api/v1/recognize/sessions    │ ← Qwen3-ASR sidecar（127.0.0.1:17946）流式字幕
└──────────────┬───────────────┘
               │ 松开快捷键（或 120s 看门狗）= finish
               ▼
┌──────────────────────────────┐
│ /api/v1/utterances            │ ← 本地 qwen3.5:4b 只纠错，不改写意思
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ jobs：claim → 按本句焦点注入 → ack │ ← IBus / 剪贴板(用完恢复) + uinput/xdotool/wtype/pynput
└──────────────────────────────┘
```

状态栏、设置页、手机页只消费同一个产品事件流 `GET /api/v1/events`。

---

## 🚀 快速开始

### 前置条件

- **Linux** (Wayland 或 X11 桌面环境)，NVIDIA GPU（Qwen3-ASR 1.7B + qwen3.5:4b 同机）
- **Python 3.10+**
- 系统工具：`xclip`（剪贴板）、`xdotool`；Wayland 需要 `xdg-desktop-portal`（GNOME 默认自带）与 `python3-dbus`，X11 需要 `maim`

```bash
# Ubuntu / Debian
sudo apt install xclip xdotool xdg-desktop-portal xdg-desktop-portal-gnome python3-dbus   # Wayland (GNOME)
sudo apt install maim xclip xdotool                                                      # X11
```

### 安装并启动

```bash
git clone https://github.com/spp417833515/voice-input.git
cd ollama-voice-input
chmod +x install.sh start.sh
./install.sh
./start.sh
```

然后打开 **https://127.0.0.1:17945/setup**：页面检测缺什么就给对应按钮——
一键安装用户级 Ollama（官方发行包校验后解压到项目缓存，不用 sudo）、拉取 `qwen3.5:4b`、下载 Qwen3-ASR 权重、启动服务。

`install.sh` 经 `_v3/units.py` 把 `systemd/*.service` 模板渲染为 4 个用户服务：

| 服务 | 作用 |
|:---|:---|
| `ollama-voice-server` | https 产品服务（v1 API + 设置页/手机页/状态栏页），端口 17945 |
| `ollama-voice-daemon` | 桌面守护进程：全局热键、录音推流、按焦点输入、截图译文浮层 |
| `ollama-voice-asr` | Qwen3-ASR sidecar，只绑 127.0.0.1:17946；权重未下载时不启动 |
| `ollama-voice-ollama` | 项目内 Ollama，只绑 127.0.0.1:11434；未安装时不启动 |

> 验收期：官方模型/真机门闩（`_activation.py` 唯一来源）未关闭时，`./start.sh`、桌面图标、`./restart.sh`
> 在任何 pip/单元同步/`systemctl start` 之前就拒绝（退出码 78），四个单元自身的 `ExecCondition` 也会跳过启动，
> 不 enable 开机自启、不拉起 ASR/Ollama。
> 真机检测时用 `./start.sh --acceptance` 显式进入验收运行（给四个单元写验收 drop-in，状态页照常列出未达门闩）；
> 之后不带参数运行 `./start.sh` 会删除 drop-in 并停止全部验收进程。
> 启动识别服务前（`./start.sh` 与设置页「启动识别服务」同一实现 `python3 -m _v3.gpu_preflight asr --unit ollama-voice-asr.service`）
> 会按单元实际参数做显存预检：vLLM 请求 `gpu_memory_utilization`（默认 0.8）× 显卡总显存，空闲不足或无法读取显存时不启动识别服务；
> 此时其余服务照常启动、设置页显示原因，`./start.sh` 以非零退出且不打印「服务已就绪」。识别服务已在运行时不预检。

### 方式三：零依赖轻量版

只需要 Ollama，不需要安装任何 Python 包：

```bash
ollama serve &
ollama pull qwen2.5:3b
python3 -m http.server 8000
# 打开 http://127.0.0.1:8000/lite/
```

> 💡 轻量版使用浏览器内置语音识别（Web Speech API），与主程序无关，仅供快速体验。

---

## ⌨️ 快捷键

### 默认快捷键

| 快捷键 | 功能 | 说明 |
|:---:|:---|:---|
| `Ctrl+Alt+Z` | **语音输入** | 按住说话，松开后自动转写并输入 |
| `Ctrl+Alt+X` | **截图翻译** | 框选区域，OCR + 翻译，浮窗显示 |
| `Ctrl+Alt+R` | **重复输入** | 重新输入上次的识别结果 |
| `Esc` | 关闭翻译浮窗 | 或点击浮窗外部自动关闭 |

> Wayland 下由 GNOME compositor 通过 `xdg-desktop-portal` 独占；X11 下由 `XGrabKey` 独占。
> 启动时会扫描桌面级冲突；`.config.json` 中仍可自定义。

### 自定义快捷键

所有快捷键均可通过 `.config.json` 配置：

```json
{
  "hotkey_voice": "ctrl+alt+z",
  "hotkey_screenshot": "ctrl+alt+x",
  "hotkey_repeat": "ctrl+alt+r"
}
```

**格式说明**：修饰键 + 按键名，用 `+` 连接，至少一个修饰键。支持的修饰键：`ctrl`、`alt`、`shift`、`super`；
设置页可录入的主键：`a`–`z`、`0`–`9`、`grave`（`` ` ``）、`space`、`tab`、`enter`、`f1`–`f12`（三条快捷键不能重复）。
仅 `shift` + 主键也可用，但会占用该键的普通输入。

也可以在设置页修改；保存后不会立即生效，设置页「桌面守护进程」一项会按当前实际状态说明：
守护进程运行中 → 运行 `./restart.sh`；未运行（例如只打开了设置页）→ 按该项给出的命令完整启动（验收期为 `./start.sh --acceptance`，门闩关闭后为 `./start.sh`）。
「快捷键冲突检查」一项列出与 GNOME 键位、其它应用全局快捷键的冲突；读取失败时显示「未能读取」，不代表无冲突。

---

## 🗣️ 识别与纠错

- 识别只用官方 Qwen3-ASR（流式字幕，松开后定稿）。
- 定稿后只交给本地 `qwen3.5:4b` 纠错（错字/标点），不扩写、不翻译；纠错失败会在状态栏报错，不悄悄回退原文。
- 截图翻译：Tesseract OCR + 本地 `qwen3.5:4b` 翻译，不调用任何在线翻译服务。

---

## 🔌 输入策略

每句话绑定按下快捷键时的焦点窗口；投递时焦点变了就不输入并提示（状态栏 `delivery_failed`）。按焦点注入时自动递降：

| 级别 | 方式 | 原理 | 适用应用 |
|:---|:---|:---|:---|
| **Tier 1** | IBus `commit_text` | 输入法级别提交，不经过剪贴板 | 支持 IBus 的所有应用 |
| **Tier 2** | 智能剪贴板粘贴 | 先保存剪贴板 → 写入 → 粘贴 → 仅当剪贴板仍是本次文本时恢复；终端 `Ctrl+Shift+V`；uinput / xdotool / wtype | 所有 GUI 应用 |
| **Tier 3** | pynput 合成按键 | X11 XTest 合成按键作为兜底 | 最大兼容性 |

**终端自动识别**：支持 26 种主流终端（gnome-terminal、konsole、alacritty、kitty、tilix、xterm 等），自动使用终端专用快捷键 `Ctrl+Shift+V`。

---

## 📁 项目结构

```
ollama-voice-input/
├── app.py               # 薄入口：uvicorn --factory app:create_production_app
├── daemon.py            # 薄入口：桌面守护进程装配 + GTK 主循环
├── _contracts.py        # v1 契约唯一来源（OpenAPI 由它生成）
├── _v3/ + _v3_app.py    # 路由、鉴权、生产装配（_v3/production.py）、单元渲染（_v3/units.py）
├── _utterance.py _jobs_service.py _polish.py _translate.py _ocr.py _asr.py ...  # 业务
├── _desktop/            # 桌面业务流程：录音会话上传、jobs 投递、重复键、截图译文
├── _linux/              # 平台：热键(portal/X11/XInput/evdev)、选区截图、粘贴与剪贴板、watchdog、托盘、WebKit 窗口
├── runtimes/asr/        # Qwen3-ASR sidecar
├── static/v3/           # 唯一界面：设置页、手机页、状态栏页、译文浮层页、openapi.json
├── screenshot_ui.py     # 独立截图翻译工具
├── start.sh install.sh restart.sh uninstall.sh
├── systemd/             # 4 个用户服务模板
└── lite/                # 零依赖轻量版
```

---

## ⚙️ 配置

配置保存在 `.config.json`，通过设置页或 `POST /api/v1/config` 修改（不返回任何密钥）。
常用字段：`ollama_model`（默认 `qwen3.5:4b`）、`hotkey_voice` / `hotkey_screenshot` / `hotkey_repeat`、`custom_keywords`、`translate_target_lang`。

---

## 🖥️ 开机自启

```bash
chmod +x install.sh
./install.sh
```

安装脚本会：
1. 写入 systemd 用户服务；**仅当生产门闩已全部关闭**才设为开机自启（验收期只写单元、不自启）
2. 创建桌面快捷方式
3. 添加到应用菜单
4. 移除旧版 autostart 双启动入口

管理命令：

```bash
# 启动/重启（从不 sudo、不 enable；单元同步只走 ./install.sh --update = 渲染 + daemon-reload）
./start.sh                # 门闩未关时拒绝并回收验收进程
./start.sh --acceptance   # 真机验收运行
./restart.sh              # 仅门闩已关或验收运行中可用

# 查看状态 / 日志
systemctl --user status ollama-voice-server ollama-voice-daemon ollama-voice-asr ollama-voice-ollama
journalctl --user -u ollama-voice-server -u ollama-voice-daemon -f

# 卸载（4 个服务全部停用并删除）
./uninstall.sh
```

---

## 🔌 API 参考

服务运行在 `https://127.0.0.1:17945`，完整契约见 `static/v3/openapi.json`（由代码生成，不手写）。

| 端点 | 方法 | 说明 |
|:---|:---|:---|
| `/api/v1/recognize/sessions` (+`/chunks` `/events` `/finish` `/cancel`) | POST/GET | 流式识别会话 |
| `/api/v1/utterances`、`/api/v1/utterances/resend` | POST | 定稿入业务 / 重复输入 |
| `/api/v1/jobs/claim`、`/api/v1/jobs/{id}/ack` | POST | 本机投递认领与确认 |
| `/api/v1/ocr-translate`、`/api/v1/translate`、`/api/v1/polish` | POST | 截图翻译 / 翻译 / 纠错 |
| `/api/v1/events`、`/api/v1/events/platform` | GET/POST | 产品事件流 / 本机平台状态上报 |
| `/api/v1/history`、`/api/v1/config`、`/api/v1/status` | GET/POST | 历史、配置、状态（含未达门闩） |
| `/api/setup/check`、`/api/setup/tasks` | GET/POST | 环境检测与一键安装作业 |
| `/api/v1/lan-access` | GET | 局域网手机访问地址与二维码 |

---

## 🔧 微信/QQ/终端 兼容

输入策略确保在所有应用中正确输入：

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

首次需在设置页下载 Qwen3-ASR 权重并拉取 qwen3.5:4b；ASR sidecar 启动加载模型需要一些时间，设置页会显示就绪状态。
</details>

<details>
<summary><b>Q: Ollama 没启动会怎样？</b></summary>

纠错与截图翻译不可用，状态栏会显示错误；不会悄悄回退原文或改用在线翻译。设置页可一键安装/启动。
</details>

<details>
<summary><b>Q: 快捷键没反应？</b></summary>

1. 确认守护进程在运行：检查悬浮状态栏是否显示
2. 某些桌面环境可能占用了默认快捷键，可在 `.config.json` 中修改
3. 守护进程有 500ms 自动刷新机制，一般稍等即可恢复
</details>

<details>
<summary><b>Q: 支持 Wayland 吗？</b></summary>

支持。Wayland 下全局热键通过 `xdg-desktop-portal` GlobalShortcuts 由 compositor 独占；截图走 portal Screenshot + 冻结选区；XWayland 仅用于 GTK 浮窗兼容。输入法级 direct commit 只在支持 `current_input_context` 的 IBus 会话中启用，`fcitx5` 等会自动回退到剪贴板粘贴链。
</details>

<details>
<summary><b>Q: 如何使用 GPU 加速？</b></summary>

Qwen3-ASR sidecar 使用独立的 vLLM 环境（`.cache/v3-feasibility/venv-asr`）直接走 GPU；`install.sh` 会按 venv 动态写入 CUDA 库路径。
</details>

<details>
<summary><b>Q: 如何修改快捷键？</b></summary>

编辑项目目录下的 `.config.json` 文件，修改 `hotkey_voice`、`hotkey_screenshot`、`hotkey_repeat` 字段。格式为 `修饰键+按键名`，例如 `"ctrl+space"`、`"alt+z"`。修改后重启守护进程。
</details>

---

## 🏗️ 技术栈

| 组件 | 技术 | 作用 |
|:---|:---|:---|
| 语音识别 | [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) 1.7B (vLLM sidecar) | 本地流式 STT |
| 文本处理 | [Ollama](https://ollama.com) + qwen3.5:4b | 本地纠错/翻译 |
| 后端框架 | [FastAPI](https://fastapi.tiangolo.com) + Uvicorn | 异步 HTTP API |
| 桌面集成 | GTK3 + WebKit2GTK + python-xlib + IBus | 全局热键、状态栏/译文浮层（同一网页界面）、输入法集成 |
| 音频采集 | sounddevice + NumPy | 实时麦克风录音 |
| 按键模拟 | evdev (uinput) / pynput | 内核级/X11 级键盘输入 |
| 截图工具 | xdg-desktop-portal (Wayland) / maim (X11) + xclip | 整屏捕获 + 冻结选区 / 选区截图 |
| OCR 引擎 | Tesseract + 本地 qwen3.5:4b | 屏幕文字识别 + 本地翻译 |

---

## 📝 更新日志

### v3.1（验收中）
- **识别换代** — 删除 Whisper，改为官方 Qwen3-ASR sidecar 流式字幕；纠错/翻译只用本地 qwen3.5:4b，删除在线翻译
- **单一 API 与界面** — 旧 `/api/transcribe`、`/api/web_input/*`、旧静态页全部删除；状态栏/设置页/手机页共用 `static/v3` 与 v1 事件流
- **按句绑定焦点** — 每句 job 绑定按键时焦点，焦点不符不输入；重复键发到当前焦点；剪贴板用完恢复
- **薄入口** — `app.py` / `daemon.py` 只装配；平台代码在 `_linux/`，桌面业务在 `_desktop/`
- **启动链** — 4 个用户服务（server/daemon/asr/ollama），https 设置页一键安装

### v3.0 (2026-10-25)
- **Wayland 按键所有权** — portal GlobalShortcuts 由 compositor 独占，原生 Activated/Deactivated；不再和 show-desktop/编辑器 redo 双触发
- **按键冲突扫描** — 启动时扫描 GNOME 桌面级快捷键，冲突显式告警
- **Wayland 截图根治** — 删除 grim/slurp 路径，改用 xdg-desktop-portal 整屏 + 自绘冻结选区 + 精确裁剪
- **快捷键默认值调整** — 语音 `Ctrl+Alt+Z`、截图 `Ctrl+Alt+X`、重复 `Ctrl+Alt+R`
- **动作层收敛** — 三种输入后端共享录音/截图/重复逻辑，不再各自复制
- **环境适配** — 支持 Python 3.14 + 新版依赖；安装脚本移除 grim/slurp，增加 python3-dbus

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
- systemd 用户服务 + 启动/重启包装脚本

---

## 📄 License

[MIT](LICENSE) — 自由使用、修改和分发。
