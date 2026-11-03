#!/bin/bash
# ==============================================================================
# QQ Agent 机器人一键启动脚本 (8080 核心 + 9880 语音服务)
# 双击此脚本即可在终端中启动 QQ Agent 机器人与语音服务
# ==============================================================================

echo -ne "\033]0;QQ Agent 机器人控制台 (Port 8080)\007"

WORKSPACE_ROOT="/Users/daijinglin/Desktop/qq-agent-workspace"
BOT_DIR="$WORKSPACE_ROOT/bot"
GSV_DIR="$WORKSPACE_ROOT/gpt-sovits"
VENV_PYTHON="$BOT_DIR/.venv/bin/python"
VENV_NB="$BOT_DIR/.venv/bin/nb"

echo "========================================================"
echo "🤖 正在启动 QQ Agent 机器人集成服务..."
echo "📂 根工作区: $WORKSPACE_ROOT"
echo "========================================================"

# 1. 检查工作目录
if [ ! -d "$BOT_DIR" ]; then
    echo "❌ 错误: 未找到机器人目录: $BOT_DIR"
    read -n 1
    exit 1
fi

# 2. 联动检查并拉起 GPT-SoVITS (9880) 语音服务
echo "🎙️ 检查 GPT-SoVITS 语音服务 (Port 9880)..."
if ! lsof -ti :9880 >/dev/null 2>&1; then
    if [ -f "$GSV_DIR/scripts/start_api.sh" ]; then
        echo "   -> 正在后台拉起 9880 语音 API..."
        bash "$GSV_DIR/scripts/start_api.sh" >/dev/null 2>&1 || true
        sleep 1.5
    fi
fi
if lsof -ti :9880 >/dev/null 2>&1; then
    echo "   ✅ 9880 语音服务在线就绪"
else
    echo "   ⚠️ 9880 语音服务未响应（将以降级文字模式运行）"
fi

cd "$BOT_DIR" || exit 1

# 3. 检查 8080 端口占用与残留 Qdrant 锁进程并优雅清理
OCCUPIED_PIDS=$( (lsof -ti :8080 2>/dev/null; lsof -t "$BOT_DIR/data/mem0_qdrant/.lock" 2>/dev/null) | sort -u | tr '\n' ' ' )
if [ -n "${OCCUPIED_PIDS// /}" ]; then
    echo "⚠️ 检测到旧进程或 Qdrant 锁占用 (PID: $OCCUPIED_PIDS)，正在释放..."
    kill -TERM $OCCUPIED_PIDS 2>/dev/null || true
    sleep 1.2
    STILL_ALIVE=$( (lsof -ti :8080 2>/dev/null; lsof -t "$BOT_DIR/data/mem0_qdrant/.lock" 2>/dev/null) | sort -u | tr '\n' ' ' )
    if [ -n "${STILL_ALIVE// /}" ]; then
        kill -9 $STILL_ALIVE 2>/dev/null || true
        sleep 0.5
    fi
    echo "✅ 旧进程与向量库文件锁已清理完毕"
fi

# 4. 强制锁定 dev 环境并启动 NoneBot 8080 服务
export ENVIRONMENT=dev
echo "🚀 正在拉起 NoneBot 核心服务 (ENVIRONMENT=dev, DRIVER=~fastapi)..."
echo "💻 Web控制台: http://127.0.0.1:8080/panel"
echo "📊 API状态:   http://127.0.0.1:8080/api/status"
echo "--------------------------------------------------------"

if [ -f "$VENV_NB" ]; then
    "$VENV_NB" run
elif [ -f "$VENV_PYTHON" ] && [ -f "$BOT_DIR/bot.py" ]; then
    "$VENV_PYTHON" "$BOT_DIR/bot.py"
elif [ -f "$VENV_PYTHON" ]; then
    "$VENV_PYTHON" -m nonebot run
else
    python3 -m nonebot run
fi

echo ""
echo "--------------------------------------------------------"
echo "🛑 机器人服务已停止。按任意键关闭此窗口..."
read -n 1
