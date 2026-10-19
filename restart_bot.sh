#!/usr/bin/env bash
set -e
ROOT_DIR="/Users/daijinglin/Desktop/qq-agent-workspace"
echo "优雅重启 8080 NoneBot 服务..."
OLD_PID=$(lsof -tiTCP:8080 -sTCP:LISTEN || true)
if [ -n "$OLD_PID" ]; then
    kill -TERM $OLD_PID 2>/dev/null || true
    sleep 2
fi
cd "$ROOT_DIR/bot"
"$ROOT_DIR/bot/.venv/bin/python" daemon_launcher.py
sleep 3
echo "重启完毕"
