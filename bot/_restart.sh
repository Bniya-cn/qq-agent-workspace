#!/bin/bash
# 终止占用 8080 的旧进程
PIDS=$(lsof -tiTCP:8080 2>/dev/null)
if [ -n "$PIDS" ]; then
    for p in $PIDS; do
        kill -9 "$p" 2>/dev/null
    done
fi

sleep 0.8

# 启动新后端
cd /Users/daijinglin/Desktop/qq-agent-workspace/bot || exit 1
export ENVIRONMENT=dev
.venv/bin/python daemon_launcher.py
