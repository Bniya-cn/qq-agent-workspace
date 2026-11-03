#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc; W=/Users/daijinglin/Desktop/qq-agent-workspace; O=$W/bot/data/test_output/exp_gagged_v5_closed
set -e
nice $R/.venv/bin/python $R/scripts/v5_v6.py
for g in '*__v6a.wav' '*__v6b.wav' '*__v6c.wav' '*__v6d.wav' '*__v6e.wav'; do ASR_GLOB="$g" nice $W/tools/asr-venv/bin/python $R/scripts/asr_dir.py $O/asr_v6.json $O; done
echo "V6 DONE $(date)"
