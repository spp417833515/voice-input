#!/bin/bash
# ============================================================================
# Ollama Voice Input — 安装 / 更新脚本
# 用法: ./install.sh            # 完整安装
#       ./install.sh --update    # 仅更新依赖并重启
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

# ---------- 卸载 ----------
if [ "$1" = "--uninstall" ]; then
    echo -e "${YELLOW}正在卸载 Ollama Voice Input ...${NC}"
    systemctl --user disable --now ollama-voice-server.service 2>/dev/null || true
    systemctl --user disable --now ollama-voice-daemon.service 2>/dev/null || true
    rm -f "$SYSTEMD_DIR/ollama-voice-server.service"
    rm -f "$SYSTEMD_DIR/ollama-voice-daemon.service"
    rm -f "$APP_MENU_DIR/$APP_NAME.desktop"
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
    command -v wl-copy &>/dev/null || MISSING_PKGS+=(wl-clipboard)
    command -v grim &>/dev/null || MISSING_PKGS+=(grim)
    command -v slurp &>/dev/null || MISSING_PKGS+=(slurp)
    command -v wtype &>/dev/null || MISSING_PKGS+=(wtype)
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

"$VENV/bin/pip" install -q --upgrade pip 2>/dev/null
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
mkdir -p "$SYSTEMD_DIR" "$LOG_DIR"

# CUDA 库路径（如有 NVIDIA GPU）
CUDA_LIBS=""
NVIDIA_LIB="$VENV/lib/python$PYTHON_VER/site-packages/nvidia"
if [ -d "$NVIDIA_LIB" ]; then
    for d in "$NVIDIA_LIB"/*/lib; do
        [ -d "$d" ] && CUDA_LIBS="$CUDA_LIBS:$d"
    done
    CUDA_LIBS="${CUDA_LIBS#:}"
fi

cat > "$SYSTEMD_DIR/ollama-voice-server.service" << EOF
[Unit]
Description=Ollama Voice Input - Web 服务器
After=network.target ollama.service
StartLimitBurst=5
StartLimitIntervalSec=60

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
${CUDA_LIBS:+Environment=LD_LIBRARY_PATH=$CUDA_LIBS}
ExecStartPre=-/usr/bin/fuser -k -TERM 17945/tcp
ExecStartPre=/bin/bash -c 'test -f $DIR/certs/cert.pem || $DIR/gen_cert.sh'
ExecStart=$VENV/bin/uvicorn app:app --host 0.0.0.0 --port 17945 --ssl-keyfile $DIR/certs/key.pem --ssl-certfile $DIR/certs/cert.pem
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_DIR/ollama-voice-daemon.service" << EOF
[Unit]
Description=Ollama Voice Input - 全局快捷键守护进程
After=ollama-voice-server.service
Wants=ollama-voice-server.service
StartLimitBurst=5
StartLimitIntervalSec=60

[Service]
# Type=notify + WatchdogSec: 主循环卡死时(不再喂狗) systemd 在 20s 后 SIGABRT 重启。
# NotifyAccess=all: sg 会 fork, python 非 MainPID, 需允许非主进程发 sd_notify。
Type=notify
NotifyAccess=all
WatchdogSec=20
WorkingDirectory=$DIR
Environment=GDK_BACKEND=x11
Environment=PYTHONUNBUFFERED=1
${CUDA_LIBS:+Environment=LD_LIBRARY_PATH=$CUDA_LIBS}
ExecStart=/bin/bash -c 'exec sg input -c "DISPLAY=\$DISPLAY GDK_BACKEND=\$GDK_BACKEND XAUTHORITY=\$XAUTHORITY WAYLAND_DISPLAY=\$WAYLAND_DISPLAY XDG_RUNTIME_DIR=\$XDG_RUNTIME_DIR XDG_SESSION_TYPE=\$XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS=\$DBUS_SESSION_BUS_ADDRESS NOTIFY_SOCKET=\$NOTIFY_SOCKET WATCHDOG_USEC=\$WATCHDOG_USEC LD_LIBRARY_PATH=\$LD_LIBRARY_PATH $VENV/bin/python3 $DIR/daemon.py"'
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable ollama-voice-server.service
systemctl --user enable ollama-voice-daemon.service
echo -e "${GREEN}  systemd 服务已注册并设为开机自启 ✓${NC}"

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
Keywords=voice;input;translate;screenshot;ollama;whisper;
EOF

# 桌面快捷方式
cp "$APP_MENU_DIR/$APP_NAME.desktop" "$DESKTOP_DIR/" 2>/dev/null || true
chmod +x "$DESKTOP_DIR/$APP_NAME.desktop" 2>/dev/null || true
# 移除旧版本的 autostart 入口，避免与 systemd 双启动
rm -f "$AUTOSTART_DIR/$APP_NAME.desktop" 2>/dev/null || true
echo -e "${GREEN}  桌面快捷方式 + 应用菜单 已配置 ✓${NC}"

# ---------- 6. 检查 Ollama ----------
echo -e "${YELLOW}[6/6] 检查 Ollama...${NC}"
if command -v ollama &>/dev/null; then
    echo -e "${GREEN}  Ollama 已安装 ✓${NC}"
else
    echo -e "${YELLOW}  Ollama 未安装，正在安装...${NC}"
    curl -fsSL https://ollama.com/install.sh | sh
fi

# ---------- 完成 ----------
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║              安装完成！                  ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}立即启动:${NC}"
echo -e "    systemctl --user start ollama-voice-server"
echo -e "    systemctl --user start ollama-voice-daemon"
echo -e "  ${GREEN}或双击桌面图标${NC}"
echo ""
echo -e "  ${GREEN}查看状态:${NC}"
echo -e "    systemctl --user status ollama-voice-server"
echo -e "    systemctl --user status ollama-voice-daemon"
echo ""
echo -e "  ${GREEN}设置页面:${NC}  http://127.0.0.1:17945/setup"
echo ""
echo -e "  ${GREEN}更新:${NC}      cd \"$DIR\" && git pull && ./install.sh"
echo -e "  ${GREEN}卸载:${NC}      ./install.sh --uninstall"
echo ""
echo -e "  ${CYAN}作者: pp   QQ: 417833515${NC}"
echo ""
