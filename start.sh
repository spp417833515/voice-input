#!/bin/bash
# Ollama Voice Input —— 一键启动
# 双击此文件运行，或在终端执行: ./start.sh

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"
LOG_DIR="$DIR/.cache/logs"
mkdir -p "$LOG_DIR"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Ollama Voice Input 启动脚本${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# ---------- 1. 检查 / 创建 venv ----------
if [ ! -f "$VENV/bin/python3" ]; then
    echo -e "${YELLOW}[1/4] 创建虚拟环境...${NC}"
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

# 检查默认模型
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:7b}"
if ! curl -sf http://127.0.0.1:11434/api/tags | python3 -c "
import sys, json
models = [m['name'] for m in json.load(sys.stdin).get('models',[])]
sys.exit(0 if any('${OLLAMA_MODEL}'.split(':')[0] in m for m in models) else 1)
" 2>/dev/null; then
    echo -e "${YELLOW}  拉取模型 $OLLAMA_MODEL...${NC}"
    ollama pull "$OLLAMA_MODEL"
fi

echo -e "${GREEN}  Ollama 就绪 ✓${NC}"

# 检查视觉模型
VISION_MODEL="${VISION_MODEL:-moondream:1.8b}"
if ! curl -sf http://127.0.0.1:11434/api/tags | python3 -c "
import sys, json
models = [m['name'] for m in json.load(sys.stdin).get('models',[])]
sys.exit(0 if any('${VISION_MODEL}'.split(':')[0] in m for m in models) else 1)
" 2>/dev/null; then
    echo -e "${YELLOW}  拉取视觉模型 $VISION_MODEL（截图翻译需要）...${NC}"
    ollama pull "$VISION_MODEL"
fi

# ---------- 4. 设置 CUDA 库路径 ----------
NVIDIA_LIB="$VENV/lib/python3.12/site-packages/nvidia"
if [ -d "$NVIDIA_LIB" ]; then
    CUDA_LIBS=""
    for d in "$NVIDIA_LIB"/*/lib; do
        [ -d "$d" ] && CUDA_LIBS="$CUDA_LIBS:$d"
    done
    export LD_LIBRARY_PATH="${CUDA_LIBS#:}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    echo -e "${GREEN}  CUDA 库路径已设置 ✓${NC}"
fi

# ---------- 5. 启动服务 ----------
echo -e "${YELLOW}[3/4] 启动 Web 服务器...${NC}"
# 先释放端口，避免冲突
fuser -k 17945/tcp 2>/dev/null || true
pkill -f "daemon.py" 2>/dev/null || true
sleep 1

"$VENV/bin/uvicorn" app:app --host 127.0.0.1 --port 17945 \
    >"$LOG_DIR/server.log" 2>&1 &
SERVER_PID=$!
sleep 2

if kill -0 $SERVER_PID 2>/dev/null; then
    echo -e "${GREEN}  Web 服务已启动: http://127.0.0.1:17945 ✓${NC}"
    echo -e "${GREEN}  配置页面: http://127.0.0.1:17945/setup ✓${NC}"
else
    echo -e "${RED}  Web 服务启动失败，查看日志: $LOG_DIR/server.log${NC}"
    exit 1
fi

# ---------- 5. 启动全局快捷键守护进程 ----------
echo -e "${YELLOW}[4/4] 启动全局快捷键守护进程...${NC}"
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
echo -e "${GREEN}  全部就绪！${NC}"
echo -e "${GREEN}  Ctrl+\`     按住说话，松开粘贴${NC}"
echo -e "${GREEN}  Alt+X       截图翻译（原位浮窗显示，右键关闭）${NC}"
echo -e "${GREEN}  日志目录:   $LOG_DIR/${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "按 Ctrl+C 停止所有服务"

# 等待退出，清理后台进程
cleanup() {
    echo ""
    echo -e "${YELLOW}正在停止服务...${NC}"
    kill $SERVER_PID 2>/dev/null
    kill $DAEMON_PID 2>/dev/null
    echo -e "${GREEN}已停止${NC}"
}
trap cleanup EXIT INT TERM

wait
