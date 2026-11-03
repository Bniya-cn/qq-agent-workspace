#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc; W=/Users/daijinglin/Desktop/qq-agent-workspace
O=$W/bot/data/test_output/exp_gagged_v3_rvc/lines_v4_amiya; PY=$R/.venv/bin/python; AS=$W/tools/asr-venv/bin/python
export PYTORCH_ENABLE_MPS_FALLBACK=1; log(){ echo "[$(date '+%F %T')] $*"; }
for i in 1 2 3; do nice $PY $R/scripts/grid.py $O/jobs_rmvpe.json 2>&1 | grep -E "GRID_DONE|Error"; done
nice $PY $R/scripts/eval_lines.py $O/eval.json | tail -1
nice $PY $R/scripts/rmvpe_f0.py | tail -1
nice $AS $R/scripts/asr_dir.py $O/asr.json $O | tail -1
log "RMVPE PASS DONE"
