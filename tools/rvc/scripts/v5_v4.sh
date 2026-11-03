#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc; W=/Users/daijinglin/Desktop/qq-agent-workspace; O=$W/bot/data/test_output/exp_gagged_v5_closed
set -e
nice $R/.venv/bin/python $R/scripts/v5_v4.py
for g in '*__v4a.wav' '*__v4b.wav' '*__v4c.wav' '*__world_noedit.wav'; do ASR_GLOB="$g" nice $W/tools/asr-venv/bin/python $R/scripts/asr_dir.py $O/asr_v4.json $O; done
echo "V4 DONE $(date)"
