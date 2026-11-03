#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc; W=/Users/daijinglin/Desktop/qq-agent-workspace
O=$W/bot/data/test_output/exp_gagged_v3_rvc/lines_v4_amiya; PY=$R/.venv/bin/python; AS=$W/tools/asr-venv/bin/python; V=$W/bot/data/test_output/exp_gagged_v4_lines
export PYTORCH_ENABLE_MPS_FALLBACK=1; log(){ echo "[$(date '+%F %T')] $*"; }
mkdir -p $O; [ -s $O/jobs.json ] || $PY $R/scripts/lines_jobs.py $O/jobs.json
for i in 1 2 3; do nice $PY $R/scripts/grid.py $O/jobs.json 2>&1 | grep -E "GRID_DONE|Error"; done
log "grid: $(ls $O/rvc_*.wav | wc -l) files"
nice $PY $R/scripts/eval_lines.py $O/eval.json
nice $AS $R/scripts/asr_dir.py $O/asr.json $O
ASR_GLOB="amiya_*_Ab_eqT*.wav" nice $AS $R/scripts/asr_dir.py $O/asr_inputs.json $V/muffled $V/g3_phrase
$PY $R/scripts/lines_report.py > /dev/null && $PY $R/scripts/report.py
log "LINES DONE"
