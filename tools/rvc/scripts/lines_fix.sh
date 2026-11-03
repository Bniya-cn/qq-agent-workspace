#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc; W=/Users/daijinglin/Desktop/qq-agent-workspace
O=$W/bot/data/test_output/exp_gagged_v3_rvc/lines_v4_amiya; PY=$R/.venv/bin/python; AS=$W/tools/asr-venv/bin/python
export PYTORCH_ENABLE_MPS_FALLBACK=1; log(){ echo "[$(date '+%F %T')] $*"; }
while pgrep -f "scripts/lines.sh" >/dev/null; do sleep 30; done
log "fix pass start"; [ -s $O/jobs_fix.json ] || $PY $R/scripts/lines_fixjobs.py
for i in 1 2 3; do nice $PY $R/scripts/grid.py $O/jobs_fix.json 2>&1 | grep -E "GRID_DONE|Error"; done
nice $PY $R/scripts/eval_lines.py $O/eval.json
nice $AS $R/scripts/asr_dir.py $O/asr.json $O
$PY $R/scripts/lines_report.py > /dev/null && $PY $R/scripts/report.py
log "LINES FIX DONE"
