#!/bin/bash
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc
EP=${1:-300}
for CH in amiya dania; do
  $R/scripts/pipeline.sh $CH train $EP > $R/logs/${CH}_train.log 2>&1
  echo "$CH exit $? $(date)" >> $R/logs/train_all.log
done
