#!/bin/bash
# Ollama Voice Input —— 快速重启 systemd 用户服务
# 用法: ./restart.sh

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVER_SERVICE="ollama-voice-server.service"
DAEMON_SERVICE="ollama-voice-daemon.service"

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
        echo -e "${YELLOW}[1/2] 同步 systemd 服务定义...${NC}"
        bash "$DIR/install.sh" --update
    fi
}

echo -e "${YELLOW}正在重启 Ollama Voice Input...${NC}"
echo ""

require_user_systemd
import_session_env
ensure_services_installed

echo -e "${YELLOW}[2/2] 重启用户服务...${NC}"
systemctl --user daemon-reload
systemctl --user restart "$SERVER_SERVICE" "$DAEMON_SERVICE"
sleep 1

if systemctl --user is-active --quiet "$SERVER_SERVICE"; then
    echo -e "${GREEN}  Web 服务已重启 ✓${NC}"
else
    echo -e "${RED}  Web 服务重启失败，请检查: systemctl --user status $SERVER_SERVICE${NC}"
    exit 1
fi

if systemctl --user is-active --quiet "$DAEMON_SERVICE"; then
    echo -e "${GREEN}  守护进程已重启 ✓${NC}"
else
    echo -e "${RED}  守护进程重启失败，请检查: systemctl --user status $DAEMON_SERVICE${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  重启完成！${NC}"
echo -e "${GREEN}  配置页面: http://127.0.0.1:17945/setup${NC}"
echo -e "${GREEN}  日志查看: journalctl --user -u $SERVER_SERVICE -u $DAEMON_SERVICE -f${NC}"
echo -e "${GREEN}========================================${NC}"
