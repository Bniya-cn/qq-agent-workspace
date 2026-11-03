#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc; W=/Users/daijinglin/Desktop/qq-agent-workspace; G=$W/bot/data/test_output/exp_gagged_v8_gen
nice $R/.venv/bin/python $R/scripts/v8_score.py
ASR_GLOB='*.wav' nice $W/tools/asr-venv/bin/python $R/scripts/asr_dir.py $G/asr_v8_rvc.json $G/rvc
ASR_GLOB='*.wav' nice $W/tools/asr-venv/bin/python $R/scripts/asr_dir.py $G/asr_v8_sovits.json $G/sovits
echo "V8 ALL DONE $(date)"
