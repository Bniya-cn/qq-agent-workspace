#!/bin/bash
# ==============================================================================
# QQ Agent 机器人一键启动脚本 (8080 服务)
# 双击此脚本即可在终端中启动 QQ Agent 机器人后台服务
# ==============================================================================

# 窗口标题
echo -ne "\033]0;QQ Agent 机器人控制台 (Port 8080)\007"

BOT_DIR="/Users/daijinglin/Desktop/qq-agent-workspace/bot"
VENV_PYTHON="$BOT_DIR/.venv/bin/python"
VENV_NB="$BOT_DIR/.venv/bin/nb"

echo "========================================================"
echo "🤖 正在启动 QQ Agent 机器人服务 (8080)..."
echo "📂 工作目录: $BOT_DIR"
echo "========================================================"

# 1. 检查工作目录是否存在
if [ ! -d "$BOT_DIR" ]; then
    echo "❌ 错误: 未找到机器人目录: $BOT_DIR"
    echo "按任意键退出..."
    read -n 1
    exit 1
fi

cd "$BOT_DIR" || exit 1

# 2. 检查 8080 端口占用情况，若已被旧进程占用则优雅释放
OCCUPIED_PID=$(lsof -ti :8080 2>/dev/null)
if [ -n "$OCCUPIED_PID" ]; then
    echo "⚠️ 检测到 8080 端口已被旧进程 (PID: $OCCUPIED_PID) 占用，正在释放..."
    kill -9 $OCCUPIED_PID 2>/dev/null
    sleep 1
    echo "✅ 旧进程已清理完毕"
fi

# 3. 激活虚拟环境并启动 NoneBot 8080 服务
echo "🚀 正在拉起 NoneBot 核心服务..."
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
    echo "⚠️ 未在 .venv 中检测到独立环境，尝试使用系统 Python..."
    python3 -m nonebot run
fi

# 退出提示
echo ""
echo "--------------------------------------------------------"
echo "🛑 机器人服务已停止。按任意键关闭此窗口..."
read -n 1
