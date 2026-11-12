#!/bin/bash
# Ollama Voice Input —— 启动 systemd 用户服务
# 用法: ./start.sh                # 正式启动：生产门闩（_activation.py）未关闭时拒绝，且回收先前验收进程
#       ./start.sh --acceptance   # 真机验收运行：门闩未关也启动，状态/设置页照常显示全部未达门闩
#
# 本脚本从不 sudo、不改用户组/udev、不 enable 开机自启（自启只由完整 ./install.sh 在门闩全关后设置）。
# 门闩判定在任何 venv/pip/单元同步/systemctl start 之前完成（fail-closed）。

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"
LOG_DIR="$DIR/.cache/logs"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVER_SERVICE="ollama-voice-server.service"
DAEMON_SERVICE="ollama-voice-daemon.service"
ASR_SERVICE="ollama-voice-asr.service"
OLLAMA_SERVICE="ollama-voice-ollama.service"
ALL_SERVICES=("$OLLAMA_SERVICE" "$ASR_SERVICE" "$SERVER_SERVICE" "$DAEMON_SERVICE")
GATED_EXIT=78
ACCEPTANCE_CONF="acceptance.conf"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

case "${1:-}" in
    "") MODE=normal ;;
    --acceptance) MODE=acceptance ;;
    *) echo "用法: ./start.sh [--acceptance]" >&2; exit 2 ;;
esac

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

acceptance_dropins_present() {
    local unit
    for unit in "${ALL_SERVICES[@]}"; do
        [ -f "$SYSTEMD_DIR/$unit.d/$ACCEPTANCE_CONF" ] && return 0
    done
    return 1
}

# 撤销验收：删 drop-in；之后由调用方停止服务，旧进程（环境里仍是 OVI_ACCEPTANCE_RUN=1）不得继续运行。
remove_acceptance_dropins() {
    local unit
    for unit in "${ALL_SERVICES[@]}"; do
        rm -f "$SYSTEMD_DIR/$unit.d/$ACCEPTANCE_CONF"
        rmdir "$SYSTEMD_DIR/$unit.d" 2>/dev/null || true
    done
    systemctl --user daemon-reload
}

# 逆序停止（daemon 先松开热键）；未安装的单元 stop 报错忽略，但停后仍在运行则入口失败。
stop_all_services() {
    local unit
    systemctl --user stop "$DAEMON_SERVICE" "$SERVER_SERVICE" "$ASR_SERVICE" "$OLLAMA_SERVICE" 2>/dev/null || true
    for unit in "${ALL_SERVICES[@]}"; do
        if systemctl --user is-active --quiet "$unit"; then
            echo -e "${RED}  $unit 未能停止，请检查: systemctl --user status $unit${NC}"
            exit 1
        fi
    done
}

write_acceptance_dropins() {
    local unit
    for unit in "${ALL_SERVICES[@]}"; do
        mkdir -p "$SYSTEMD_DIR/$unit.d"
        printf '[Service]\nEnvironment=OVI_ACCEPTANCE_RUN=1\n' > "$SYSTEMD_DIR/$unit.d/$ACCEPTANCE_CONF"
    done
}

# 本地模型单元带 ConditionPathExists：未安装/未下载时 systemd 跳过启动，这里如实提示去设置页，不算失败；
# 其它启动失败让入口以非零退出，不继续宣布就绪。
start_model_service() {
    local service="$1"
    local label="$2"

    if systemctl --user is-active --quiet "$service"; then
        echo -e "${GREEN}  $label 已在运行 ✓${NC}"
        return 0
    fi
    systemctl --user start "$service" || true
    if [ "$(systemctl --user show -p ConditionResult --value "$service" 2>/dev/null)" = "no" ]; then
        echo -e "${YELLOW}  $label 未安装/未下载，请在设置页完成（https://127.0.0.1:17945/setup）${NC}"
        return 0
    fi
    if systemctl --user is-active --quiet "$service"; then
        echo -e "${GREEN}  $label 已启动 ✓（模型加载可能需要几分钟，设置页显示就绪状态）${NC}"
        return 0
    fi
    echo -e "${RED}  $label 启动失败，请检查: journalctl --user -u $service${NC}"
    return 1
}

# 识别服务新启动前的显存预检（与设置页「启动识别服务」同一实现 _v3.gpu_preflight，按单元真实 ExecStart 参数）。
# 已在运行则不预检；预检不通过（显存不足或无法判断）则不启动 ASR，由调用方记为失败项，其余服务照常启动。
start_asr_service() {
    local label="Qwen3-ASR 识别"
    local out

    if systemctl --user is-active --quiet "$ASR_SERVICE"; then
        echo -e "${GREEN}  $label 已在运行 ✓${NC}"
        return 0
    fi
    if ! out="$(cd "$DIR" && "$VENV/bin/python3" -m _v3.gpu_preflight asr --unit "$ASR_SERVICE" 2>&1)"; then
        echo -e "${RED}  $label 未启动：$out${NC}"
        return 1
    fi
    echo -e "${GREEN}  $out${NC}"
    start_model_service "$ASR_SERVICE" "$label"
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

# ---------- 0. 生产门闩（fail-closed，先于一切副作用）----------
if [ "$MODE" = "normal" ]; then
    HAD_ACCEPTANCE=0
    acceptance_dropins_present && HAD_ACCEPTANCE=1
    remove_acceptance_dropins
    if ! env -u OVI_ACCEPTANCE_RUN python3 "$DIR/_activation.py" check >/dev/null; then
        # 门闩未关：此刻在跑的本项目服务只可能是验收残留，一律停止（不抓键、不占 GPU）。
        stop_all_services
        echo -e "${RED}  未启动任何服务、未设开机自启。${NC}"
        exit "$GATED_EXIT"
    fi
    if [ "$HAD_ACCEPTANCE" -eq 1 ]; then
        echo -e "${YELLOW}  撤销验收运行：先停止验收进程，再按正式模式启动${NC}"
        stop_all_services
    fi
else
    echo -e "${YELLOW}  验收运行：官方/真机门闩仍未关闭，状态与设置页会如实显示未达项。${NC}"
    OVI_ACCEPTANCE_RUN=1 python3 "$DIR/_activation.py" check >/dev/null
fi

mkdir -p "$LOG_DIR"
import_session_env

# ---------- 1. 检查 / 创建 venv ----------
if [ ! -f "$VENV/bin/python3" ]; then
    echo -e "${YELLOW}  创建虚拟环境...${NC}"
    python3 -m venv --system-site-packages "$VENV"
fi

# ---------- 2. 安装依赖 ----------
echo -e "${YELLOW}[1/3] 检查 Python 依赖...${NC}"
"$VENV/bin/pip" install -q -r "$DIR/requirements.txt"

# ---------- 3. 同步单元（只渲染 + daemon-reload，不 sudo、不 enable）----------
echo -e "${YELLOW}[2/3] 同步 systemd 服务定义...${NC}"
bash "$DIR/install.sh" --update
if [ "$MODE" = "acceptance" ]; then
    write_acceptance_dropins
    systemctl --user daemon-reload
fi
import_session_env

# ---------- 4. 启动 systemd 用户服务 ----------
# 纠错/翻译只用本地 qwen3.5:4b（ollama-voice-ollama），识别只用 Qwen3-ASR sidecar（ollama-voice-asr）。
echo -e "${YELLOW}[3/3] 启动用户服务...${NC}"
FAILED=()
start_model_service "$OLLAMA_SERVICE" "本地 Ollama (qwen3.5:4b)"
start_asr_service || FAILED+=("Qwen3-ASR 识别")
start_service "$SERVER_SERVICE" "Web 服务"
start_service "$DAEMON_SERVICE" "守护进程"

echo ""
if [ "${#FAILED[@]}" -gt 0 ]; then
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  未全部就绪，未启动: ${FAILED[*]}${NC}"
    echo -e "${RED}  原因见上方；设置页: https://127.0.0.1:17945/setup${NC}"
    echo -e "${RED}  日志查看:   journalctl --user -u $ASR_SERVICE${NC}"
    echo -e "${RED}========================================${NC}"
    exit 1
fi
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  服务已就绪！${NC}"
echo -e "${GREEN}  配置页面: https://127.0.0.1:17945/setup${NC}"
echo -e "${GREEN}  Ctrl+Alt+Z   按住说话，松开输入${NC}"
echo -e "${GREEN}  Ctrl+Alt+X   截图翻译${NC}"
echo -e "${GREEN}  Ctrl+Alt+R   重复输入${NC}"
echo -e "${GREEN}  日志查看:   journalctl --user -u $SERVER_SERVICE -u $DAEMON_SERVICE -u $ASR_SERVICE -u $OLLAMA_SERVICE -f${NC}"
echo -e "${GREEN}========================================${NC}"
