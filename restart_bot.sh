#!/usr/bin/env bash
set -e
ROOT_DIR="/Users/daijinglin/Desktop/qq-agent-workspace"
echo "优雅重启 8080 NoneBot 服务..."
OLD_PID=$( (lsof -tiTCP:8080 -sTCP:LISTEN 2>/dev/null; lsof -t "$ROOT_DIR/bot/data/mem0_qdrant/.lock" 2>/dev/null) | sort -u | tr '\n' ' ' )
if [ -n "${OLD_PID// /}" ]; then
    kill -TERM $OLD_PID 2>/dev/null || true
    sleep 1.5
    STILL_PID=$( (lsof -tiTCP:8080 -sTCP:LISTEN 2>/dev/null; lsof -t "$ROOT_DIR/bot/data/mem0_qdrant/.lock" 2>/dev/null) | sort -u | tr '\n' ' ' )
    if [ -n "${STILL_PID// /}" ]; then
        kill -9 $STILL_PID 2>/dev/null || true
        sleep 0.5
    fi
fi
cd "$ROOT_DIR/bot"
export ENVIRONMENT=dev
"$ROOT_DIR/bot/.venv/bin/python" daemon_launcher.py
sleep 3
echo "NoneBot 8080 服务重启完毕"
