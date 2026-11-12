#!/bin/bash
# Ollama Voice Input —— 快速重启 systemd 用户服务（server + daemon）
# 用法: ./restart.sh
#
# 只在“门闩已关闭”或“当前处于 ./start.sh --acceptance 验收运行（存在验收 drop-in）”时重启；
# 否则 fail-closed 拒绝（不同步、不 restart）。从不 sudo、不 enable。单元同步只走 install.sh --update。

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVER_SERVICE="ollama-voice-server.service"
DAEMON_SERVICE="ollama-voice-daemon.service"
GATED_EXIT=78

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

echo -e "${YELLOW}正在重启 Ollama Voice Input...${NC}"
echo ""

require_user_systemd

# ---------- 0. 生产门闩（fail-closed）----------
if [ -f "$SYSTEMD_DIR/$SERVER_SERVICE.d/acceptance.conf" ]; then
    echo -e "${YELLOW}  验收运行中：官方/真机门闩仍未关闭${NC}"
    OVI_ACCEPTANCE_RUN=1 python3 "$DIR/_activation.py" check >/dev/null
elif ! env -u OVI_ACCEPTANCE_RUN python3 "$DIR/_activation.py" check >/dev/null; then
    echo -e "${RED}  未重启任何服务。${NC}"
    exit "$GATED_EXIT"
fi

import_session_env
echo -e "${YELLOW}[1/2] 同步 systemd 服务定义...${NC}"
bash "$DIR/install.sh" --update

echo -e "${YELLOW}[2/2] 重启用户服务...${NC}"
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
echo -e "${GREEN}  配置页面: https://127.0.0.1:17945/setup${NC}"
echo -e "${GREEN}  日志查看: journalctl --user -u $SERVER_SERVICE -u $DAEMON_SERVICE -f${NC}"
echo -e "${GREEN}========================================${NC}"
