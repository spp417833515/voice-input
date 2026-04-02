#!/bin/bash
# Ollama Voice Input —— 快速重启
# 用法: ./restart.sh

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"
LOG_DIR="$DIR/.cache/logs"
mkdir -p "$LOG_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YELLOW}正在重启 Ollama Voice Input...${NC}"
echo ""

# ---------- 1. 停止旧进程 ----------
echo -e "${YELLOW}[1/3] 停止旧进程...${NC}"
fuser -k 17945/tcp 2>/dev/null || true
pkill -f "daemon.py" 2>/dev/null || true
sleep 1
echo -e "${GREEN}  旧进程已停止 ✓${NC}"

# ---------- 2. 设置 CUDA 库路径 ----------
NVIDIA_LIB="$VENV/lib/python3.12/site-packages/nvidia"
if [ -d "$NVIDIA_LIB" ]; then
    CUDA_LIBS=""
    for d in "$NVIDIA_LIB"/*/lib; do
        [ -d "$d" ] && CUDA_LIBS="$CUDA_LIBS:$d"
    done
    export LD_LIBRARY_PATH="${CUDA_LIBS#:}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    echo -e "${GREEN}  CUDA 库路径已设置 ✓${NC}"
fi

# ---------- 3. 启动服务 ----------
echo -e "${YELLOW}[2/3] 启动 Web 服务器...${NC}"
"$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 17945 \
    >"$LOG_DIR/server.log" 2>&1 &
SERVER_PID=$!
sleep 2

if kill -0 $SERVER_PID 2>/dev/null; then
    echo -e "${GREEN}  Web 服务已启动: http://127.0.0.1:17945 ✓${NC}"
else
    echo -e "${RED}  Web 服务启动失败，查看日志: $LOG_DIR/server.log${NC}"
    exit 1
fi

echo -e "${YELLOW}[3/3] 启动全局快捷键守护进程...${NC}"
"$VENV/bin/python3" "$DIR/daemon.py" >"$LOG_DIR/daemon.log" 2>&1 &
DAEMON_PID=$!
sleep 1

if kill -0 $DAEMON_PID 2>/dev/null; then
    echo -e "${GREEN}  守护进程已启动 ✓${NC}"
else
    echo -e "${RED}  守护进程启动失败，查看日志: $LOG_DIR/daemon.log${NC}"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  重启完成！${NC}"
echo -e "${GREEN}  Ctrl+\`     按住说话，松开粘贴${NC}"
echo -e "${GREEN}  Alt+X       截图翻译${NC}"
echo -e "${GREEN}  日志目录:   $LOG_DIR/${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "按 Ctrl+C 停止所有服务"

cleanup() {
    echo ""
    echo -e "${YELLOW}正在停止服务...${NC}"
    kill $SERVER_PID 2>/dev/null
    kill $DAEMON_PID 2>/dev/null
    echo -e "${GREEN}已停止${NC}"
}
trap cleanup EXIT INT TERM

wait
