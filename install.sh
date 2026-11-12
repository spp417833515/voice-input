#!/bin/bash
# ============================================================================
# Ollama Voice Input — 安装 / 更新脚本
# 用法: ./install.sh            # 完整安装（系统包/sudo/input 组/udev/依赖/单元/快捷方式；
#                                #   仅当生产门闩已关闭才 enable 开机自启，见 _activation.py）
#       ./install.sh --update    # 安全同步：只渲染 systemd 用户单元 + daemon-reload。
#                                #   不 sudo、不装包、不改组/udev、不 pip、不 enable、不 start/restart
#       ./install.sh --uninstall # 卸载
#
# 作者: pp  QQ: 417833515
# ============================================================================

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="ollama-voice-input"
VENV="$DIR/.venv"
PYTHON_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
LOG_DIR="$DIR/.cache/logs"
ICON_SRC="$DIR/resources/ollama-voice-input.svg"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/桌面")"
APP_MENU_DIR="$HOME/.local/share/applications"
SYSTEMD_DIR="$HOME/.config/systemd/user"
ICON_INST_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
AUTOSTART_DIR="$HOME/.config/autostart"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

UNITS=(ollama-voice-ollama.service ollama-voice-asr.service ollama-voice-server.service ollama-voice-daemon.service)

# 单元模板在 systemd/*.service，唯一渲染器 _v3/units.py（不调用 systemctl）。
# server：v1 API + static/v3（https）；daemon：直接 exec python（禁止 sg input，portal 需要可 dumpable）；
# asr：Qwen3-ASR sidecar 127.0.0.1:17946（权重就绪才启动）；ollama：项目内用户级 Ollama 127.0.0.1:11434。
# 只写文件 + daemon-reload：不 enable、不 start。完整安装与 --update 共用这一处。
render_units() {
    if [ ! -x "$VENV/bin/python3" ]; then
        echo -e "${RED}  缺少 $VENV，请先运行完整安装 ./install.sh${NC}"
        exit 1
    fi
    mkdir -p "$SYSTEMD_DIR" "$LOG_DIR"
    # CUDA 库路径（如有 NVIDIA GPU）
    local cuda_libs="" nvidia_lib d
    nvidia_lib="$VENV/lib/python$PYTHON_VER/site-packages/nvidia"
    if [ -d "$nvidia_lib" ]; then
        for d in "$nvidia_lib"/*/lib; do
            [ -d "$d" ] && cuda_libs="$cuda_libs:$d"
        done
        cuda_libs="${cuda_libs#:}"
    fi
    (cd "$DIR" && "$VENV/bin/python3" -m _v3.units --project-dir "$DIR" --venv "$VENV" --cuda-libs "$cuda_libs" --out-dir "$SYSTEMD_DIR")
    systemctl --user daemon-reload
}

# ---------- 安全同步（start.sh / restart.sh 调用）----------
if [ "$1" = "--update" ]; then
    render_units
    echo -e "${GREEN}  systemd 用户单元已同步（未 enable / 未启动）✓${NC}"
    exit 0
fi

# ---------- 卸载 ----------
if [ "$1" = "--uninstall" ]; then
    echo -e "${YELLOW}正在卸载 Ollama Voice Input ...${NC}"
    for unit in "${UNITS[@]}"; do
        systemctl --user disable --now "$unit" 2>/dev/null || true
        rm -f "$SYSTEMD_DIR/$unit"
        rm -rf "$SYSTEMD_DIR/$unit.d"
    done
    rm -f "$APP_MENU_DIR/$APP_NAME.desktop"
    rm -f "$APP_MENU_DIR/ollama-voice-input-daemon.desktop"
    rm -f "$AUTOSTART_DIR/$APP_NAME.desktop"
    rm -f "$DESKTOP_DIR/$APP_NAME.desktop"
    rm -f "$ICON_INST_DIR/$APP_NAME.svg"
    systemctl --user daemon-reload 2>/dev/null || true
    echo -e "${GREEN}卸载完成 ✓${NC}"
    echo -e "  项目文件保留在: $DIR"
    echo -e "  如需完全删除: rm -rf \"$DIR\""
    exit 0
fi

echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   Ollama Voice Input 安装程序           ║${NC}"
echo -e "${CYAN}║   作者: pp   QQ: 417833515              ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ---------- 1. 系统依赖 ----------
echo -e "${YELLOW}[1/6] 检查系统依赖...${NC}"

MISSING_PKGS=()

# GTK3 + GObject Introspection (核心依赖)
if ! python3 -c "import gi" 2>/dev/null; then
    MISSING_PKGS+=(python3-gi gir1.2-gtk-3.0)
fi

# AppIndicator3 (系统托盘)
if ! python3 -c "
import gi
try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3
except:
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3
" 2>/dev/null; then
    MISSING_PKGS+=(gir1.2-ayatanaappindicator3-0.1)
fi

# 截图/剪贴板工具 (按会话类型)
SESSION_TYPE="${XDG_SESSION_TYPE:-x11}"
if [ "$SESSION_TYPE" = "wayland" ]; then
    # 截图走 xdg-desktop-portal（GNOME 不支持 grim/slurp 的 wlr 接口）；
    # 剪贴板优先 xclip（Mutter 桥接），wl-clipboard/wtype 作为兜底。
    command -v xclip &>/dev/null || MISSING_PKGS+=(xclip)
    command -v wl-copy &>/dev/null || MISSING_PKGS+=(wl-clipboard)
    command -v wtype &>/dev/null || MISSING_PKGS+=(wtype)
    python3 -c "import dbus" 2>/dev/null || MISSING_PKGS+=(python3-dbus)
else
    command -v xclip &>/dev/null || MISSING_PKGS+=(xclip)
    command -v maim &>/dev/null || MISSING_PKGS+=(maim)
fi
command -v xdotool &>/dev/null || MISSING_PKGS+=(xdotool)

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo -e "${YELLOW}  安装缺少的系统包: ${MISSING_PKGS[*]}${NC}"
    if command -v apt &>/dev/null; then
        sudo apt install -y "${MISSING_PKGS[@]}"
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y "${MISSING_PKGS[@]}"
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm "${MISSING_PKGS[@]}"
    else
        echo -e "${RED}  无法自动安装，请手动安装: ${MISSING_PKGS[*]}${NC}"
    fi
fi
echo -e "${GREEN}  系统依赖就绪 ✓${NC}"

# ---------- 1.5 确保用户属于 input 组（evdev 快捷键支持）----------
if ! groups "$USER" | grep -qw input; then
    echo -e "${YELLOW}  添加用户到 input 组（evdev 全局快捷键需要）...${NC}"
    sudo usermod -aG input "$USER"
    echo -e "${GREEN}  已添加 $USER 到 input 组（重新登录后生效）${NC}"
    NEED_RELOGIN=1
else
    echo -e "${GREEN}  用户已在 input 组 ✓${NC}"
fi

# udev 规则: 让 input 组可以访问 /dev/uinput（内核级按键模拟）
if [ ! -f /etc/udev/rules.d/99-uinput.rules ]; then
    echo -e "${YELLOW}  配置 uinput 设备权限...${NC}"
    echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules > /dev/null
    sudo udevadm control --reload-rules 2>/dev/null
    sudo udevadm trigger /dev/uinput 2>/dev/null
    echo -e "${GREEN}  uinput 权限已配置 ✓${NC}"
fi

# ---------- 2. Python 虚拟环境 + 依赖 ----------
echo -e "${YELLOW}[2/6] 安装 Python 依赖...${NC}"

if [ ! -f "$VENV/bin/python3" ]; then
    python3 -m venv --system-site-packages "$VENV"
    echo -e "${GREEN}  虚拟环境已创建${NC}"
fi

"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$DIR/requirements.txt"
echo -e "${GREEN}  Python 依赖就绪 ✓${NC}"

# ---------- 3. 安装图标 ----------
echo -e "${YELLOW}[3/6] 安装应用图标...${NC}"
mkdir -p "$ICON_INST_DIR"
if [ -f "$ICON_SRC" ]; then
    cp "$ICON_SRC" "$ICON_INST_DIR/$APP_NAME.svg"
    echo -e "${GREEN}  图标已安装 ✓${NC}"
else
    echo -e "${YELLOW}  图标文件不存在，使用系统默认图标${NC}"
fi

# ---------- 4. 生成 systemd 服务 ----------
echo -e "${YELLOW}[4/6] 配置 systemd 用户服务...${NC}"
render_units
if env -u OVI_ACCEPTANCE_RUN python3 "$DIR/_activation.py" check >/dev/null 2>&1; then  # 只有官方门闩全关才自启；验收不自启
    for unit in "${UNITS[@]}"; do
        systemctl --user enable "$unit"
    done
    echo -e "${GREEN}  systemd 服务已注册并设为开机自启 ✓${NC}"
else
    # 门闩未关：只写单元，不设开机自启（单元自身 ExecCondition 也会拒启）；验收用 ./start.sh --acceptance。
    echo -e "${YELLOW}  systemd 单元已写入；生产门闩未关闭，未设开机自启、未启动：${NC}"
    env -u OVI_ACCEPTANCE_RUN python3 "$DIR/_activation.py" check || true
fi

# ---------- 5. 生成 .desktop 文件 ----------
echo -e "${YELLOW}[5/6] 创建桌面快捷方式...${NC}"
mkdir -p "$APP_MENU_DIR"

ICON_REF="$ICON_INST_DIR/$APP_NAME.svg"
[ ! -f "$ICON_REF" ] && ICON_REF="audio-input-microphone"

cat > "$APP_MENU_DIR/$APP_NAME.desktop" << EOF
[Desktop Entry]
Name=Ollama Voice Input
Name[zh_CN]=语音输入助手
Comment=全局语音输入 + 截图翻译，完全本地运行
Comment[zh_CN]=全局语音输入 + 截图翻译，完全本地运行
Exec=bash -c 'cd "$DIR" && ./start.sh'
Terminal=false
Type=Application
Icon=$ICON_REF
Categories=Utility;Audio;Accessibility;
StartupNotify=false
Keywords=voice;input;translate;screenshot;ollama;qwen;asr;
EOF

# daemon 专用 desktop 文件：portal 需要 app id，而 systemd 启动没有终端/desktop 上下文。
# Host Registry 会读取这个 desktop 文件，把 daemon 识别成对应 app id。
cat > "$APP_MENU_DIR/ollama-voice-input-daemon.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Ollama Voice Input Daemon
Comment=Global hotkeys and screenshot translation for Ollama Voice Input
Exec=$VENV/bin/python3 $DIR/daemon.py
Terminal=false
NoDisplay=true
Categories=Utility;
EOF

# 桌面快捷方式
cp "$APP_MENU_DIR/$APP_NAME.desktop" "$DESKTOP_DIR/" 2>/dev/null || true
chmod +x "$DESKTOP_DIR/$APP_NAME.desktop" 2>/dev/null || true
# 移除旧版本的 autostart 入口，避免与 systemd 双启动
rm -f "$AUTOSTART_DIR/$APP_NAME.desktop" 2>/dev/null || true
echo -e "${GREEN}  桌面快捷方式 + 应用菜单 已配置 ✓${NC}"

# ---------- 6. 本地模型（Ollama / Qwen3-ASR）----------
# 不再用 curl | sh 做系统级安装：设置页「一键安装」把官方 Ollama 发行包校验后解压到项目缓存（不用 sudo），
# 并拉取 qwen3.5:4b、下载 Qwen3-ASR 权重；完成后由 ollama-voice-ollama / ollama-voice-asr 用户单元运行。
echo -e "${YELLOW}[6/6] 本地模型...${NC}"
echo -e "${GREEN}  打开设置页完成 Ollama(qwen3.5:4b) 与 Qwen3-ASR 安装/下载: https://127.0.0.1:17945/setup${NC}"

# ---------- 完成 ----------
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║              安装完成！                  ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}立即启动:${NC}"
echo -e "    ./start.sh"
echo -e "  ${GREEN}或双击桌面图标${NC}"
echo ""
echo -e "  ${GREEN}查看状态:${NC}"
echo -e "    systemctl --user status ollama-voice-server"
echo -e "    systemctl --user status ollama-voice-daemon"
echo ""
echo -e "  ${GREEN}设置页面:${NC}  https://127.0.0.1:17945/setup"
echo ""
echo -e "  ${GREEN}更新:${NC}      cd \"$DIR\" && git pull && ./install.sh"
echo -e "  ${GREEN}卸载:${NC}      ./install.sh --uninstall"
echo ""
echo -e "  ${CYAN}作者: pp   QQ: 417833515${NC}"
echo ""
