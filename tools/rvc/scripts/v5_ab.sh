#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc
W=/Users/daijinglin/Desktop/qq-agent-workspace
O=$W/bot/data/test_output/exp_gagged_v5_closed
set -e
nice $R/.venv/bin/python $R/scripts/v5_ab.py
# ASR on before + v1mild + v2medium only (skip discarded v0 __closed*)
ASR_GLOB="*__before.wav" nice $W/tools/asr-venv/bin/python $R/scripts/asr_dir.py $O/asr_v1v2.json $O
ASR_GLOB="*__v1mild.wav" nice $W/tools/asr-venv/bin/python $R/scripts/asr_dir.py $O/asr_v1v2.json $O
ASR_GLOB="*__v2medium.wav" nice $W/tools/asr-venv/bin/python $R/scripts/asr_dir.py $O/asr_v1v2.json $O
nice $R/.venv/bin/python $R/scripts/v5_report.py
echo "V5_AB DONE $(date)"
