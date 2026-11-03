#!/bin/bash
# usage: smoke.sh [chars...]  -> uses latest saved weights of each model
R=/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc
W=/Users/daijinglin/Desktop/qq-agent-workspace
S=$W/bot/data/test_output/exp_gagged_v2/src_nospeech
O=$W/bot/data/test_output/exp_gagged_v3_rvc; mkdir -p $O
cd $R/applio; export PYTORCH_ENABLE_MPS_FALLBACK=1
for CH in ${@:-amiya dania}; do
  M=${CH}_gag_rvc
  PTH=$(ls -t logs/$M/${M}_*e_*s.pth 2>/dev/null | head -1)
  [ -z "$PTH" ] && { echo "no weights for $M"; continue; }
  IDX=$(ls logs/$M/*.index | head -1); TAG=$(basename $PTH .pth | sed "s/${M}_//")
  for n in 1 2 3; do
    SRC=$S/gagged_${n}_bs_vocals_nospeech.wav; [ $n = 3 ] && [ -f $S/gagged_3_bs_vocals_nospeech_v2.wav ] && SRC=$S/gagged_3_bs_vocals_nospeech_v2.wav
    # requested small shifts + "target" shift that moves source RMVPE median F0 (g1 693 / g2 593 / g3 356 Hz) into the character range (amiya ~290, dania ~230 Hz)
    if [ $CH = dania ]; then case $n in 1) PITCHES="-3 -19";; 2) PITCHES="-3 -16";; 3) PITCHES="-3 -8";; esac
    else case $n in 1) PITCHES="-2 -15";; 2) PITCHES="-2 -12";; 3) PITCHES="-2 -4";; esac; fi
    for P in $PITCHES; do for IR in ${IRS:-0.4 0.6}; do for PR in ${PRS:-0.5}; do for VE in ${VES:-0.0 0.25}; do
      OUT=$O/rvc_${CH}_g${n}_p${P}_ir${IR}_pr${PR}_rms${VE}_${TAG}.wav
      [ -f $OUT ] && continue
      nice $R/.venv/bin/python core.py infer --input-path $SRC --output-path $OUT --pth-path $PWD/$PTH --index-path $PWD/$IDX \
        --pitch $P --index-rate $IR --protect $PR --f0-method rmvpe --volume-envelope $VE --embedder-model contentvec --export-format WAV > /dev/null 2>&1 \
        && echo "OK $OUT" || echo "FAIL $OUT"
    done; done; done; done
  done
done
