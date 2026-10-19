#!/bin/bash
cd /Users/daijinglin/Desktop/qq-agent-workspace/bot || exit 1
LOG=bot.restart.log
while true; do
  echo "[$(date '+%H:%M:%S')] keep-alive launching" >> "$LOG"
  .venv/bin/python bot.py >> "$LOG" 2>&1 &
  CHILD=$!
  echo "$CHILD" > bot.pid
  echo "[$(date '+%H:%M:%S')] keep-alive child=$CHILD" >> "$LOG"
  wait "$CHILD"
  EC=$?
  echo "[$(date '+%H:%M:%S')] keep-alive child exit=$EC — restart in 2s" >> "$LOG"
  sleep 2
done
