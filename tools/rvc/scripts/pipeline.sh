#!/bin/bash
# usage: pipeline.sh <char> <stage: prep|train|all> [epochs]
set -o pipefail
CH=$1; ST=${2:-all}; EP=${3:-300}
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc
M=${CH}_gag_rvc
cd $R/applio
PY=$R/.venv/bin/python
export PYTORCH_ENABLE_MPS_FALLBACK=1
if [ "$ST" = prep ] || [ "$ST" = all ]; then
  echo "== preprocess $(date)"
  $PY core.py preprocess --model-name $M --dataset-path $R/datasets/$CH --sample-rate 40000 --cpu-cores 8 --cut-preprocess Automatic --chunk-len 3.0 --overlap-len 0.3 --normalization-mode post || exit 1
  echo "== extract $(date)"
  $PY core.py extract --model-name $M --f0-method rmvpe --cpu-cores 8 --gpu 0 --sample-rate 40000 --embedder-model contentvec --include-mutes 2 || exit 1
fi
if [ "$ST" = train ] || [ "$ST" = all ]; then
  echo "== train $(date)"
  $PY core.py train --model-name $M --vocoder HiFi-GAN --save-every-epoch 10 --save-only-latest --total-epoch $EP --sample-rate 40000 --batch-size 8 --gpu 0 --pretrained --index-algorithm Auto || exit 1
  echo "== done $(date)"
fi
