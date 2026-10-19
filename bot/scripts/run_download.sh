#!/bin/bash
cd /Users/daijinglin/Desktop/qq-agent-workspace/bot || exit 1
.venv/bin/python scripts/download_character_refs.py --games all > download.log 2>&1 &
echo $! > download.pid
