#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc; W=/Users/daijinglin/Desktop/qq-agent-workspace; O=$W/bot/data/test_output/exp_gagged_v5_closed
nice $R/.venv/bin/python $R/scripts/v5_run.py
ASR_GLOB="*.wav" nice $W/tools/asr-venv/bin/python $R/scripts/asr_dir.py $O/asr.json $O
echo "V5 DONE $(date)"
