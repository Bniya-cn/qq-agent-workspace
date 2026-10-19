#!/bin/bash
cd "/Users/daijinglin/Desktop/qq-agent-workspace/bot" || exit 78
export ENVIRONMENT=dev
exec "/Users/daijinglin/Desktop/qq-agent-workspace/bot/.venv/bin/python" "/Users/daijinglin/Desktop/qq-agent-workspace/bot/bot.py"
