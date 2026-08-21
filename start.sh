#!/bin/bash
# Ollama Voice Input —— 启动 systemd 用户服务
# 双击此文件运行，或在终端执行: ./start.sh

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"
LOG_DIR="$DIR/.cache/logs"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVER_SERVICE="ollama-voice-server.service"
DAEMON_SERVICE="ollama-voice-daemon.service"
mkdir -p "$LOG_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

require_user_systemd() {
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        echo -e "${RED}未检测到 systemd 用户会话，请在桌面会话中运行此脚本。${NC}"
        exit 1
    fi
}

import_session_env() {
    systemctl --user import-environment \
        DISPLAY \
        XAUTHORITY \
        WAYLAND_DISPLAY \
        XDG_RUNTIME_DIR \
        XDG_SESSION_TYPE \
        DBUS_SESSION_BUS_ADDRESS >/dev/null 2>&1 || true
}

ensure_services_installed() {
    local needs_install=0

    [ ! -f "$SYSTEMD_DIR/$SERVER_SERVICE" ] && needs_install=1
    [ ! -f "$SYSTEMD_DIR/$DAEMON_SERVICE" ] && needs_install=1
    [ "$DIR/install.sh" -nt "$SYSTEMD_DIR/$SERVER_SERVICE" ] && needs_install=1
    [ "$DIR/install.sh" -nt "$SYSTEMD_DIR/$DAEMON_SERVICE" ] && needs_install=1
    [ "$DIR/systemd/$SERVER_SERVICE" -nt "$SYSTEMD_DIR/$SERVER_SERVICE" ] && needs_install=1
    [ "$DIR/systemd/$DAEMON_SERVICE" -nt "$SYSTEMD_DIR/$DAEMON_SERVICE" ] && needs_install=1

    if [ "$needs_install" -eq 1 ]; then
        echo -e "${YELLOW}[3/4] 同步 systemd 服务定义...${NC}"
        bash "$DIR/install.sh" --update
    else
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user enable "$SERVER_SERVICE" "$DAEMON_SERVICE" >/dev/null 2>&1 || true
    fi
}

start_service() {
    local service="$1"
    local label="$2"

    if systemctl --user is-active --quiet "$service"; then
        echo -e "${GREEN}  $label 已在运行 ✓${NC}"
        return 0
    fi

    systemctl --user start "$service"
    sleep 1
    if systemctl --user is-active --quiet "$service"; then
        echo -e "${GREEN}  $label 已启动 ✓${NC}"
        return 0
    fi

    echo -e "${RED}  $label 启动失败，请检查: systemctl --user status $service${NC}"
    return 1
}

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Ollama Voice Input 启动脚本${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

require_user_systemd
import_session_env

# ---------- 1. 检查 / 创建 venv ----------
if [ ! -f "$VENV/bin/python3" ]; then
    echo -e "${YELLOW}  创建虚拟环境...${NC}"
    python3 -m venv --system-site-packages "$VENV"
fi

# ---------- 2. 安装依赖 ----------
echo -e "${YELLOW}[1/4] 检查 Python 依赖...${NC}"
"$VENV/bin/pip" install -q -r "$DIR/requirements.txt" 2>/dev/null

# ---------- 3. 检查 Ollama ----------
echo -e "${YELLOW}[2/4] 检查 Ollama...${NC}"
if ! command -v ollama &>/dev/null; then
    echo -e "${RED}  Ollama 未安装，正在安装...${NC}"
    curl -fsSL https://ollama.com/install.sh | sh
fi

if ! curl -sf http://127.0.0.1:11434/api/tags &>/dev/null; then
    echo -e "${YELLOW}  启动 Ollama 服务...${NC}"
    if systemctl is-active ollama &>/dev/null; then
        :
    else
        systemctl start ollama 2>/dev/null || nohup ollama serve >"$LOG_DIR/ollama.log" 2>&1 &
        sleep 3
    fi
fi

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:7b}"
if ! curl -sf http://127.0.0.1:11434/api/tags | OLLAMA_MODEL="$OLLAMA_MODEL" python3 -c "
import sys, json, os
model = os.environ['OLLAMA_MODEL']
models = [m['name'] for m in json.load(sys.stdin).get('models', [])]
sys.exit(0 if any(model.split(':')[0] in m for m in models) else 1)
" 2>/dev/null; then
    echo -e "${YELLOW}  拉取模型 $OLLAMA_MODEL...${NC}"
    ollama pull "$OLLAMA_MODEL"
fi

echo -e "${GREEN}  Ollama 就绪 ✓${NC}"

ensure_services_installed
import_session_env

# ---------- 4. 启动 systemd 用户服务 ----------
echo -e "${YELLOW}[4/4] 启动用户服务...${NC}"
start_service "$SERVER_SERVICE" "Web 服务"
start_service "$DAEMON_SERVICE" "守护进程"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  服务已就绪！${NC}"
echo -e "${GREEN}  配置页面: http://127.0.0.1:17945/setup${NC}"
echo -e "${GREEN}  Ctrl+\`     按住说话，松开粘贴${NC}"
echo -e "${GREEN}  Alt+X       截图翻译${NC}"
echo -e "${GREEN}  日志查看:   journalctl --user -u $SERVER_SERVICE -u $DAEMON_SERVICE -f${NC}"
echo -e "${GREEN}========================================${NC}"
