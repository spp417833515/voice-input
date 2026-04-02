#!/bin/bash
# 安装开机自启服务 + 桌面快捷方式
# 运行: ./install.sh

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}安装 Ollama Voice Input 服务${NC}"

# ---------- 1. 安装 systemd 用户服务 ----------
echo -e "${YELLOW}[1/3] 安装 systemd 用户服务...${NC}"
mkdir -p ~/.config/systemd/user

cp "$DIR/systemd/ollama-voice-server.service" ~/.config/systemd/user/
cp "$DIR/systemd/ollama-voice-daemon.service" ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable ollama-voice-server.service
systemctl --user enable ollama-voice-daemon.service

echo -e "${GREEN}  服务已注册并设为开机自启 ✓${NC}"

# ---------- 2. 创建桌面快捷方式 ----------
echo -e "${YELLOW}[2/3] 创建桌面快捷方式...${NC}"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/桌面")"
cp "$DIR/ollama-voice-input.desktop" "$DESKTOP_DIR/" 2>/dev/null || true
chmod +x "$DESKTOP_DIR/ollama-voice-input.desktop" 2>/dev/null || true
echo -e "${GREEN}  桌面快捷方式已创建 ✓${NC}"

# ---------- 3. 安装到应用菜单 ----------
echo -e "${YELLOW}[3/3] 添加到应用菜单...${NC}"
mkdir -p ~/.local/share/applications
cp "$DIR/ollama-voice-input.desktop" ~/.local/share/applications/
echo -e "${GREEN}  已添加到应用菜单 ✓${NC}"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  安装完成！${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN}  立即启动:${NC}"
echo -e "${GREEN}    systemctl --user start ollama-voice-server${NC}"
echo -e "${GREEN}    systemctl --user start ollama-voice-daemon${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN}  或直接双击桌面图标${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN}  查看状态:${NC}"
echo -e "${GREEN}    systemctl --user status ollama-voice-server${NC}"
echo -e "${GREEN}    systemctl --user status ollama-voice-daemon${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN}  卸载:${NC}"
echo -e "${GREEN}    systemctl --user disable --now ollama-voice-server${NC}"
echo -e "${GREEN}    systemctl --user disable --now ollama-voice-daemon${NC}"
echo -e "${GREEN}========================================${NC}"
