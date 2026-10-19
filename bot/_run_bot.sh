#!/bin/bash
cd /Users/daijinglin/Desktop/qq-agent-workspace/bot || exit 1
echo "[$(date '+%H:%M:%S')] start wrapper pid=$$" >> bot.restart.log
.venv/bin/python bot.py >> bot.restart.log 2>&1 &
CHILD=$!
echo "$CHILD" > bot.pid
echo "[$(date '+%H:%M:%S')] python child=$CHILD" >> bot.restart.log
wait "$CHILD"
EC=$?
echo "[$(date '+%H:%M:%S')] child exit=$EC" >> bot.restart.log
exit "$EC"
