#!/bin/bash
# Overnight orchestrator: waits for identity training, then fidelity grid -> eval/ASR -> gag model -> chains -> report
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc; W=/Users/daijinglin/Desktop/qq-agent-workspace
O=$W/bot/data/test_output/exp_gagged_v3_rvc; PY=$R/.venv/bin/python; AS=$W/tools/asr-venv/bin/python
export PYTORCH_ENABLE_MPS_FALLBACK=1
log(){ echo "[$(date '+%F %T')] $*"; }
log "waiting for train_all (identity models)"; while pgrep -f "scripts/train_all.sh" >/dev/null; do sleep 60; done
log "train_all finished: $(cat $R/logs/train_all.log 2>/dev/null | tr '\n' ' ')"
cd $R/applio && for m in amiya dania; do $PY core.py index --model-name ${m}_gag_rvc --index-algorithm Auto | tail -1; done
# ---- (2) fidelity grid on identity models
$PY $R/scripts/make_jobs.py fidelity $O/jobs_fidelity.json
for i in 1 2 3 4 5; do $PY $R/scripts/grid.py $O/jobs_fidelity.json 2>&1 | grep -E "GRID_DONE|Error" ; done
log "fidelity grid done: $(ls $O/fidelity/*.wav | wc -l) files"
nice $PY $R/scripts/eval.py $O/eval_fidelity.json $O/fidelity > $R/logs/eval_fidelity.log 2>&1
nice $AS $R/scripts/asr_dir.py $O/asr_fidelity.json $O/fidelity > $R/logs/asr_fidelity.log 2>&1
$PY $R/scripts/select_top.py $O/eval_fidelity.json $O/asr_fidelity.json rvc_ $O/fidelity_top.json
log "fidelity selection done"
# ---- (1b) separate gag texture model (muffled data only, never mixed into identity sets)
cd $R/applio
$PY core.py preprocess --model-name gag_rvc --dataset-path $R/datasets_gag/gag --sample-rate 40000 --cpu-cores 8 --cut-preprocess Automatic --chunk-len 3.0 --overlap-len 0.3 --normalization-mode post | tail -1
$PY core.py extract --model-name gag_rvc --f0-method rmvpe --cpu-cores 8 --gpu 0 --sample-rate 40000 --embedder-model contentvec --include-mutes 2 | tail -1
$PY core.py train --model-name gag_rvc --vocoder HiFi-GAN --save-every-epoch 50 --save-only-latest --total-epoch 200 --sample-rate 40000 --batch-size 4 --gpu 0 --pretrained --index-algorithm Auto > $R/logs/gag_train.log 2>&1
log "gag model trained: $(ls $R/applio/logs/gag_rvc/*.pth $R/applio/logs/gag_rvc/*.index 2>/dev/null | tr '\n' ' ')"
$PY $R/scripts/make_jobs.py chain $O/jobs_chain.json
for i in 1 2 3; do $PY $R/scripts/grid.py $O/jobs_chain.json 2>&1 | grep -E "GRID_DONE|Error"; done
nice $PY $R/scripts/eval.py $O/eval_chain.json $O/chain > $R/logs/eval_chain.log 2>&1
nice $AS $R/scripts/asr_dir.py $O/asr_chain.json $O/chain > $R/logs/asr_chain.log 2>&1
$PY $R/scripts/report.py > $R/logs/report.log 2>&1
log "ALL DONE -> $O/report.md"
