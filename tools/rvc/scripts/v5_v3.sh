#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc; W=/Users/daijinglin/Desktop/qq-agent-workspace; O=$W/bot/data/test_output/exp_gagged_v5_closed
set -e
nice $R/.venv/bin/python $R/scripts/v5_v3.py
for g in '*__v3a.wav' '*__v3b.wav' '*__v3c.wav' '*__v3d.wav' '*__closed_emo.wav'; do ASR_GLOB="$g" nice $W/tools/asr-venv/bin/python $R/scripts/asr_dir.py $O/asr_v3.json $O; done
echo "V3 DONE $(date)"
