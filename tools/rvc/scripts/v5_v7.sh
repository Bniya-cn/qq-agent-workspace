#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc; W=/Users/daijinglin/Desktop/qq-agent-workspace; O=$W/bot/data/test_output/exp_gagged_v5_closed
set -e
nice $R/.venv/bin/python $R/scripts/v5_v7.py
for g in '*__v7a.wav' '*__v7b.wav' '*__v7c.wav' '*__v7d.wav' '*__v7e.wav' '*__v7f.wav' '*__v7g.wav' '*S?_seedvc*__before.wav'; do ASR_GLOB="$g" nice $W/tools/asr-venv/bin/python $R/scripts/asr_dir.py $O/asr_v7.json $O; done
echo "V7 DONE $(date)"
